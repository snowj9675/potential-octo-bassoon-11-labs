#!/usr/bin/env python3
"""Whole-dialogue Eleven v3 generation with tag-directed delivery and phrase subtitles."""
import argparse
import base64
import json
import math
from pathlib import Path
import re
import podcast as core


def untag(text):
    return re.sub(r'\[[^\]]*\]',lambda m:' '*len(m.group()),text)


def words(text):
    return [m.group().casefold() for m in re.finditer(r"[^\W_]+(?:['’-][^\W_]+)*",untag(text))]


def load_script(path):
    episode=json.loads(Path(path).read_text())
    if episode['level'] not in ('A1','A2') or not episode['turns']:
        raise ValueError('Provide an A1/A2 dialogue with turns')
    for turn in episode['turns']:
        if turn['speaker'] not in ('male','female') or not turn['chunks']:
            raise ValueError('Each turn needs a male/female speaker and translated chunks')
        if words(turn['text']) != words(' '.join(c['de'] for c in turn['chunks'])):
            raise ValueError('Caption German must match all spoken words exactly, with audio tags removed')
        for c in turn['chunks']:
            if not 1 <= len(c['de'].split()) <= 5 or not 1 <= len(c['en'].split()) <= 6:
                raise ValueError('Keep German captions to 5 words and translations to 6 words')
            if any(x in c['de']+c['en'] for x in ('\n','[',']')):
                raise ValueError('Caption phrases cannot contain tags or line breaks')
    return episode


def body_for(episode,voices):
    return dict(model_id='eleven_v3',language_code='de',inputs=[dict(text=t['text'],voice_id=voices[t['speaker']]) for t in episode['turns']])


def paths_for(body):
    ident=core.cache_id(body,'dialogue-v3-mp3_44100_128')
    return core.CACHE/f'{ident}.json',core.CACHE/f'{ident}.pending'


def credit_plan(body,config,accounts=None):
    count=sum(len(i['text']) for i in body['inputs'])
    if count > 2000:
        raise ValueError('For reliable v3 dialogue, keep this test block at or below 2,000 characters including tags')
    target,marker=paths_for(body)
    if marker.exists() and not target.exists():
        raise RuntimeError('Uncertain prior dialogue request; inspect ElevenLabs history before retrying')
    cost=0 if target.exists() else math.ceil(count*config['credit_ceiling_per_character'])
    report=dict(model='eleven_v3',german_and_tag_characters=count,cached=target.exists(),credit_upper_estimate=cost,
                note='Includes audio tags. Model/voice rates can vary; no Flash discount assumed. English captions are not synthesized.')
    if accounts is not None:
        selected=next((a for a in accounts if a['remaining']>=cost),None)
        if selected is None:
            raise RuntimeError('One account must have enough quota for the complete dialogue request')
        report['accounts']=[dict(label=a['name'],remaining=a['remaining'],planned_upper_estimate=cost if a is selected else 0) for a in accounts]
    core.atomic(core.OUT/'credit-plan.json',report)
    print(json.dumps(report,indent=2),flush=True)
    if cost > config['max_credits']:
        raise RuntimeError('Dialogue exceeds run credit budget; no synthesis performed')
    return report


def generate(body,config,accounts,mode):
    target,marker=paths_for(body)
    if target.exists():
        return json.loads(target.read_text())
    if mode!='generate':
        raise RuntimeError('Render mode cannot synthesize; restore the dialogue cache or choose generate')
    cost=math.ceil(sum(len(i['text']) for i in body['inputs'])*config['credit_ceiling_per_character'])
    for a in accounts:
        sub=core.request(a['key'],'/user/subscription')
        remaining=max(0,sub['character_limit']-sub['character_count'])
        if remaining<cost:
            continue
        core.atomic(marker,dict(account=a['name'],model='eleven_v3',characters=sum(len(i['text']) for i in body['inputs'])))
        try:
            result=core.request(a['key'],'/text-to-dialogue/with-timestamps?output_format=mp3_44100_128',body)
        except core.ProviderError as e:
            if e.status in ('quota_exceeded','insufficient_credits'):
                marker.unlink()
                print(f"Quota exhausted on {a['name']}; moving the whole request to the next account",flush=True)
                continue
            if e.code==429 and e.status in ('rate_limited','too_many_concurrent_requests'):
                marker.unlink()
            raise
        core.atomic(target,result)  # Save paid audio before alignment or rendering can fail.
        marker.unlink()
        return result
    raise RuntimeError('No account can fund the complete dialogue')


def subtitle_cues(episode,result,audio_duration):
    alignment=result.get('alignment') or result.get('normalized_alignment')
    if not alignment:
        raise RuntimeError('Cached dialogue lacks character alignment; no audio will be regenerated')
    text=''.join(alignment['characters'])
    starts=alignment['character_start_times_seconds']
    ends=alignment['character_end_times_seconds']
    if len(text)!=len(starts) or len(text)!=len(ends):
        raise RuntimeError('Incomplete provider alignment')
    tokens=list(re.finditer(r"[^\W_]+(?:['’-][^\W_]+)*",untag(text)))
    expected=[word for t in episode['turns'] for word in words(t['text'])]
    if [m.group().casefold() for m in tokens]!=expected:
        raise RuntimeError('Provider alignment differs from script; paid audio remains cached for subtitle repair')
    cues=[]
    cursor=0
    for turn in episode['turns']:
        for c in turn['chunks']:
            n=len(words(c['de']))
            first,last=tokens[cursor],tokens[cursor+n-1]
            cues.append(dict(start=starts[first.start()],end=ends[last.end()-1],speaker=turn['speaker'],**c))
            cursor+=n
    for i,cue in enumerate(cues):
        next_start=cues[i+1]['start'] if i+1<len(cues) else audio_duration
        cue['end']=min(next_start,cue['end']+0.35,audio_duration)
        if cue['start']>=cue['end']:
            raise RuntimeError('Invalid phrase timing; refusing to guess caption times')
    return cues


def srt_time(t):
    n=round(t*1000)
    return f'{n//3600000:02}:{n//60000%60:02}:{n//1000%60:02},{n%1000:03}'


def render(episode,result,config):
    core.OUT.mkdir(exist_ok=True)
    (core.OUT/'podcast.mp3').write_bytes(base64.b64decode(result['audio_base64'],validate=True))
    core.run('ffmpeg','-v','error','-y','-i',core.OUT/'podcast.mp3','-ar','44100','-ac','1',core.OUT/'podcast.wav')
    length=core.duration(core.OUT/'podcast.wav')
    cues=subtitle_cues(episode,result,length)
    # WrapStyle 2 and short authored phrases keep each language to one visual line.
    header='''[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 2
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: German,DejaVu Sans,52,&H00FFFFFF,&H00FFFFFF,&H00181B25,&H00000000,-1,0,0,0,100,100,0,0,1,3,1,8,130,130,65,1
Style: English,DejaVu Sans,36,&H008DE7FF,&H008DE7FF,&H00181B25,&H00000000,0,0,0,0,100,100,0,0,1,2,1,8,130,130,145,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    events=[]
    for lang,style in [('de','German'),('en','English')]:
        srt=[]
        for i,cue in enumerate(cues,1):
            events.append(f"Dialogue: 0,{core.ass_time(cue['start'])},{core.ass_time(cue['end'])},{style},,0,0,0,,{core.escape(cue[lang])}")
            srt.append(f"{i}\n{srt_time(cue['start'])} --> {srt_time(cue['end'])}\n{cue[lang]}\n")
        (core.OUT/('german.srt' if lang=='de' else 'english.srt')).write_text('\n'.join(srt))
    (core.OUT/'subtitles.ass').write_text(header+'\n'.join(events)+'\n')
    core.atomic(core.OUT/'timeline.json',dict(model='eleven_v3',duration_seconds=length,manual_pauses=False,playback_speed=1.0,waveform=False,cues=cues))
    core.run('ffmpeg','-v','error','-y','-loop','1','-framerate','25','-i',config['image'],'-i',core.OUT/'podcast.mp3',
             '-vf','scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,ass=output/subtitles.ass',
             '-c:v','libx264','-preset','fast','-crf','21','-pix_fmt','yuv420p','-c:a','aac','-b:a','160k',
             '-t',str(length),'-movflags','+faststart',core.OUT/'podcast.mp4')
    core.run('ffmpeg','-v','error','-y','-ss','1','-i',core.OUT/'podcast.mp4','-frames:v','1',core.OUT/'preview.png')
    print(f'Rendered v3 dialogue: {length:.1f}s; {len(cues)} short bilingual cues; no manual pauses or waveform',flush=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--script',default='input/v3-learning-german.json')
    parser.add_argument('--mode',choices=['plan','render','generate'],default='plan')
    parser.add_argument('--max-credits',type=int,default=700)
    args=parser.parse_args()
    episode=load_script(args.script)
    config=json.loads(Path('config.json').read_text())
    config['max_credits']=args.max_credits
    if config['credit_ceiling_per_character']<1:
        raise ValueError('Use a conservative credit ceiling of at least 1')
    voices={g:config[g+'_voice_id'] for g in ('male','female')}
    accounts=None
    if args.mode!='plan' and not Path(config['image']).is_file():
        raise FileNotFoundError(config['image'])
    if args.mode=='generate':
        accounts,voices=core.discover(core.keys(),config)
    body=body_for(episode,voices)
    credit_plan(body,config,accounts)
    if args.mode=='plan':
        return
    result=generate(body,config,accounts,args.mode)
    render(episode,result,config)


if __name__=='__main__':main()
