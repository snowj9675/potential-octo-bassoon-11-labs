#!/usr/bin/env python3
"""German dialogue renderer. No paid calls in plan or mock modes."""
import argparse
import base64
import hashlib
import json
import math
import os
import re
from pathlib import Path
import subprocess
import textwrap
import urllib.error
import urllib.request

API = 'https://api.elevenlabs.io/v1'
CACHE = Path('.audio-cache')
OUT = Path('output')
SETTINGS = dict(stability=0.65, similarity_boost=0.75, style=0, use_speaker_boost=True, speed=0.90)


def run(*args):
    subprocess.run([str(x) for x in args], check=True, stdout=subprocess.DEVNULL)


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    tmp.replace(path)


def load_episode(path):
    data = json.loads(Path(path).read_text())
    assert data['level'] in ('A1', 'A2'), 'level must be A1 or A2'
    lines = data['lines']
    assert lines, 'Empty episode'
    for i, line in enumerate(lines):
        assert line['speaker'] in ('male', 'female')
        assert 0 < len(line['de']) <= 180, 'Use short subtitle turns (max 180 characters)'
        assert 0 < len(line['en']) <= 220
        assert '\n' not in line['de'] and '\n' not in line['en']
        sentence_pairs(line)
        selected_pause(line)
        if 'sentence_pauses' in line:
            assert len(line['sentence_pauses']) == len(sentences(line['de'])), 'One sentence_pauses value per sentence'
            assert all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) and 0 <= v <= 3 for v in line['sentence_pauses'])
    return data


def request(key, route, body=None):
    req = urllib.request.Request(API + route, data=None if body is None else json.dumps(body).encode(),
                                 headers={'xi-api-key': key, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get('detail', {})
            status = detail.get('status', '') if isinstance(detail, dict) else ''
        except (ValueError, AttributeError):
            status = ''
        # Never log provider response bodies or keys.
        raise ProviderError(e.code, status) from None
    except (OSError, ValueError):
        raise RuntimeError('Ambiguous provider response. No automatic retry; inspect ElevenLabs history before clearing pending cache.') from None


class ProviderError(Exception):
    def __init__(self, code, status):
        self.code, self.status = code, status
        super().__init__(f'ElevenLabs HTTP {code}, status {status or "unknown"}')


def keys():
    # JSON secret is extensible without exposing all repository secrets to this job.
    found = {n: os.environ[n] for n in ('ALVIN', 'AMY_OLIVER', 'FLORINA', 'JOHN_SNOW') if os.getenv(n)}
    extra = json.loads(os.getenv('ELEVENLABS_KEYS_JSON') or '{}')
    if not isinstance(extra, dict) or any(not isinstance(v, str) or not v for v in extra.values()):
        raise ValueError('ELEVENLABS_KEYS_JSON must be a JSON object of label: API-key')
    found.update(extra)
    unique, seen = [], set()
    for name, key in found.items():
        if key not in seen:
            unique.append(dict(name=name, key=key))
            seen.add(key)
    return unique


def discover(accounts, config):
    usable = []
    for account in accounts:
        try:
            sub = request(account['key'], '/user/subscription')
            voices = request(account['key'], '/voices')['voices']
            # Multiple keys for the same workspace do not create extra quota.
            user = request(account['key'], '/user')
            uid = user.get('user_id')
            if not uid:
                raise RuntimeError('User ID unavailable; cannot safely deduplicate account balances')
            if any(a['uid'] == uid for a in usable):
                continue
            account.update(uid=uid, remaining=max(0, sub['character_limit']-sub['character_count']),
                           voices={v['voice_id']: v for v in voices})
            usable.append(account)
        except ProviderError as e:
            print(f"Account {account['name']}: unavailable ({e.code}); skipped")
    if not usable:
        raise RuntimeError('No usable API keys. Keys need user/subscription, user and voices read access plus text-to-speech permission.')
    selection_path = CACHE / 'voice-selection.json'
    saved = json.loads(selection_path.read_text()) if selection_path.exists() else {}
    selected = {}
    for gender in ('male', 'female'):
        override = config.get(gender + '_voice_id') or saved.get(gender)
        if override:
            selected[gender] = override
            continue
        candidates = {}
        for account in usable:
            for vid, v in account['voices'].items():
                labels = v.get('labels') or {}
                if labels.get('gender', '').lower() != gender:
                    continue
                # Native German-labelled voices preferred; standard voices remain multilingual.
                score = (10 if any('german' in str(x).lower() or str(x).lower() == 'de' for x in labels.values()) else 0)
                score += 2 if v.get('category') == 'premade' else 0
                candidates[vid] = max(candidates.get(vid, -1), score)
        if not candidates:
            raise RuntimeError(f'No {gender} voice available; set {gender}_voice_id in config.json')
        selected[gender] = max(sorted(candidates), key=lambda vid: (candidates[vid], sum(vid in a['voices'] for a in usable)))
    # Require both identities on every failover account, preventing a voice change mid-episode.
    usable = [a for a in usable if all(v in a['voices'] for v in selected.values())]
    if not usable:
        raise RuntimeError('Selected male/female voices must both be available on at least one account')
    atomic(selection_path, selected)
    print('Selected voice IDs:', json.dumps(selected))
    return usable, selected


def payload(line, config, voices):
    return dict(text=line['de'], model_id=config['model'], language_code='de',
                voice_settings=SETTINGS), voices[line['speaker']]


def cache_id(body, voice):
    return hashlib.sha256(json.dumps([body, voice], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def plan(lines, config, voices, accounts=None):
    seen, pending, cached = set(), [], 0
    for line in lines:
        body, voice = payload(line, config, voices)
        ident = cache_id(body, voice)
        if ident in seen:
            continue
        seen.add(ident)
        if (CACHE / f'{ident}.json').exists():
            cached += len(line['de'])
        else:
            if (CACHE / f'{ident}.pending').exists():
                raise RuntimeError(f'Uncertain previous generation {ident}: check ElevenLabs history before retrying. Recover audio or remove this pending marker only after checking billing.')
            pending.append(len(line['de']))
    # Conservative ceiling: Flash advertised discount varies by plan/voice.
    bound = sum(math.ceil(n * config['credit_ceiling_per_character']) for n in pending)
    report = dict(total_german_characters=sum(len(x['de']) for x in lines), cached_characters=cached,
                  uncached_unique_characters=sum(pending), credit_upper_estimate=bound,
                  advertised_flash_estimate=math.ceil(sum(pending)*0.5),
                  note='Estimate, not a billing quote. Only German is synthesized. Conservative ceiling defaults to 1 credit/character; voice multipliers may cost more.')
    if accounts is not None:
        balances = [a['remaining'] for a in accounts]
        assigned = [0]*len(accounts)
        for n in pending:
            cost = math.ceil(n * config['credit_ceiling_per_character'])
            idx = next((i for i, b in enumerate(balances) if b >= cost), None)
            if idx is None:
                raise RuntimeError('Insufficient balance for complete episode under conservative estimate; no speech generated')
            balances[idx] -= cost
            assigned[idx] += cost
        report['accounts'] = [dict(label=a['name'], remaining=a['remaining'], planned_upper_estimate=c, estimated_remaining=b) for a,c,b in zip(accounts,assigned,balances)]
    atomic(OUT / 'credit-plan.json', report)
    print(json.dumps(report, indent=2))
    if bound > config['max_credits']:
        raise RuntimeError(f'Credit budget exceeded ({bound} > {config["max_credits"]}); no speech generated')
    return report


def synthesize(line, config, voices, accounts):
    body, voice = payload(line, config, voices)
    ident = cache_id(body, voice)
    target, marker = CACHE / f'{ident}.json', CACHE / f'{ident}.pending'
    if target.exists():
        return json.loads(target.read_text())
    if marker.exists():
        raise RuntimeError('Uncertain generation blocked; inspect pending cache marker')
    cost = math.ceil(len(line['de'])*config['credit_ceiling_per_character'])
    for account in accounts:
        if account['remaining'] < cost:
            continue
        # Refresh before every billable turn, including after account failover.
        sub = request(account['key'], '/user/subscription')
        account['remaining'] = max(0, sub['character_limit']-sub['character_count'])
        if account['remaining'] < cost:
            continue
        atomic(marker, dict(account=account['name'], characters=len(line['de']), voice_id=voice))
        try:
            result = request(account['key'], f'/text-to-speech/{voice}/with-timestamps?output_format=mp3_44100_128', body)
        except ProviderError as e:
            if e.status in ('quota_exceeded', 'insufficient_credits'):
                marker.unlink()
                account['remaining'] = 0
                print(f"Quota reached on {account['name']}; moving to next account")
                continue
            # Auth, policy/abuse blocks, throttling and server errors are NOT rotated around.
            raise
        # Persist the full paid response before decoding/rendering, so failures cannot rebill it.
        atomic(target, result)
        marker.unlink()
        account['remaining'] -= cost
        return result
    raise RuntimeError('No account can fund the next turn. Completed turns are saved.')


def sentences(text):
    """Input uses short A1/A2 sentences; avoid splitting common abbreviations."""
    spans = []
    begin = 0
    for match in re.finditer(r'[.!?]+(?:[»”"\']?)(?=\s+|$)', text):
        end = match.end()
        word = text[begin:end].split()[-1].lower()
        if word in {'z.', 'b.', 'dr.', 'nr.', 'bzw.', 'usw.'} or re.fullmatch(r'[A-ZÄÖÜ]\.', text[begin:end].split()[-1]):
            continue
        spans.append((begin, end))
        begin = end
        while begin < len(text) and text[begin].isspace():
            begin += 1
    if begin < len(text):
        spans.append((begin, len(text)))
    return spans or [(0, len(text))]


def generation_units(line, config, voices):
    """Reuse an existing full turn; synthesize only new material sentence by sentence."""
    body, voice = payload(line, config, voices)
    ident = cache_id(body, voice)
    if (CACHE / f'{ident}.json').exists() or (CACHE / f'{ident}.pending').exists():
        return [line]
    return [dict(line, de=line['de'][a:b]) for a, b in sentences(line['de'])]


def sentence_intervals(text, alignment, audio_duration):
    """Partition all original samples; character timing puts cuts between sentences."""
    spans = sentences(text)
    if len(spans) == 1:
        return [(0.0, audio_duration)]
    if not alignment or ''.join(alignment.get('characters', [])) != text:
        raise RuntimeError('Exact character alignment required to pause cached multi-sentence audio; no audio regenerated.')
    starts = alignment['character_start_times_seconds']
    ends = alignment['character_end_times_seconds']
    if len(starts) != len(text) or len(ends) != len(text):
        raise RuntimeError('Incomplete cached alignment; no audio regenerated.')
    cuts = [0.0]
    for previous, current in zip(spans, spans[1:]):
        # Whitespace and punctuation are timed; keep the boundary inside their gap.
        cut = (ends[previous[1]-1] + starts[current[0]]) / 2
        if not cuts[-1] < cut < audio_duration:
            raise RuntimeError('Invalid cached sentence timing; refusing to cut speech')
        cuts.append(cut)
    cuts.append(audio_duration)
    return list(zip(cuts, cuts[1:]))


PAUSES = {'quick': 0.15, 'normal': 0.35, 'question': 0.50, 'example': 0.75, 'reflect': 0.95}


def sentence_pairs(line):
    german = [line['de'][a:b] for a,b in sentences(line['de'])]
    english = [line['en'][a:b] for a,b in sentences(line['en'])]
    if len(german) != len(english):
        raise ValueError('German and English must have the same sentence count; supply one sentence and its translation per line.')
    return list(zip(german,english))


def selected_pause(line, text=None):
    if 'pause_after' in line:
        value = line['pause_after']
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not 0 <= value <= 3:
            raise ValueError('pause_after must be 0–3 seconds')
        return value
    if 'pace' in line:
        if line['pace'] not in PAUSES:
            raise ValueError('pace must be quick, normal, question, example or reflect')
        return PAUSES[line['pace']]
    text = text or line['de']
    if text.endswith('?'):
        return PAUSES['question']
    if len(text.split()) <= 4:
        return PAUSES['quick']
    return PAUSES['normal']


def pace_turn(sources, index, config, line):
    """Keep one bilingual sentence per cue; use script-directed rhythm."""
    pairs = sentence_pairs(line)
    parts = []
    for source, text, alignment in sources:
        for start, end in sentence_intervals(text, alignment, duration(source)):
            parts.append((source, start, end))
    if len(parts) != len(pairs):
        raise ValueError('Sentence audio and bilingual subtitle counts differ')
    rendered, cues, cursor = [], [], 0.0
    for n, ((source, start, end),(de,en)) in enumerate(zip(parts,pairs)):
        target = OUT / f'paced-{index:04}-{n:03}.wav'
        pause = 0
        if n < len(parts)-1:
            pause = line['sentence_pauses'][n] if 'sentence_pauses' in line else selected_pause({'de':de})
        filt = f'atrim=start={start}:end={end},asetpts=PTS-STARTPTS,atempo={config.get("playback_speed", 0.9)},apad=pad_dur={pause}'
        run('ffmpeg','-v','error','-y','-i',source,'-af',filt,'-ar','44100','-ac','1',target)
        length = duration(target)
        cues.append(dict(start=cursor,end=cursor+length,speaker=line['speaker'],de=de,en=en,pause_after=pause))
        cursor += length
        rendered.append(target)
    manifest = OUT / f'paced-{index:04}.txt'
    manifest.write_text(''.join(f"file '{p.name}'\n" for p in rendered))
    target = OUT / f'paced-{index:04}.wav'
    run('ffmpeg','-v','error','-y','-f','concat','-safe','0','-i',manifest,'-c:a','pcm_s16le',target)
    return target, cues


def duration(path):
    return float(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration','-of','csv=p=0',str(path)]))


def ass_time(seconds):
    n = round(seconds*100)
    return f'{n//360000}:{n//6000%60:02}:{n//100%60:02}.{n%100:02}'


def escape(text):
    return text.replace('\\', '＼').replace('{', '｛').replace('}', '｝')


def render(episode, clips, image):
    OUT.mkdir(exist_ok=True)
    timeline, cursor = [], 0.0
    for i, (line, (audio, cues)) in enumerate(zip(episode['lines'],clips)):
        gap = line['sentence_pauses'][-1] if 'sentence_pauses' in line else selected_pause(line, cues[-1]['de'])
        clip = OUT / f'turn-{i:04}.wav'
        run('ffmpeg','-v','error','-y','-i',audio,'-af',f'apad=pad_dur={gap}','-ar','44100','-ac','1',clip)
        length = duration(clip)
        for n,cue in enumerate(cues):
            end = cue['end'] + (gap if n==len(cues)-1 else 0)
            timeline.append((cursor+cue['start'],cursor+end,dict(speaker=cue['speaker'],de=cue['de'],en=cue['en'],pause_after=gap if n==len(cues)-1 else cue['pause_after'])))
        cursor += length
    (OUT/'concat.txt').write_text(''.join(f"file 'turn-{i:04}.wav'\n" for i in range(len(clips))))
    run('ffmpeg','-v','error','-y','-f','concat','-safe','0','-i',OUT/'concat.txt','-af','loudnorm=I=-18:TP=-2:LRA=7','-ar','44100',OUT/'podcast.wav')
    run('ffmpeg','-v','error','-y','-i',OUT/'podcast.wav','-c:a','libmp3lame','-b:a','128k',OUT/'podcast.mp3')
    header = '''[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: German,DejaVu Sans,52,&H00FFFFFF,&H00FFFFFF,&H00181B25,&H90000000,-1,0,0,0,100,100,0,0,1,3,1,8,180,180,55,1
Style: English,DejaVu Sans,36,&H008DE7FF,&H008DE7FF,&H00181B25,&H90000000,0,0,0,0,100,100,0,0,1,2,1,8,180,180,165,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    events, de_srt, en_srt = [], [], []
    def srt_time(t):
        ms=round(t*1000)
        return f'{ms//3600000:02}:{ms//60000%60:02}:{ms//1000%60:02},{ms%1000:03}'
    for i,(start,end,line) in enumerate(timeline,1):
        for style,lang,width in [('German','de',49),('English','en',69)]:
            txt = r'\N'.join(escape(s) for s in textwrap.wrap(line[lang],width))
            events.append(f'Dialogue: 0,{ass_time(start)},{ass_time(end)},{style},,0,0,0,,{txt}')
        for lang, dest in [('de',de_srt),('en',en_srt)]:
            dest.append(f'{i}\n{srt_time(start)} --> {srt_time(end)}\n{line[lang]}\n')
    (OUT/'subtitles.ass').write_text(header+'\n'.join(events)+'\n')
    (OUT/'german.srt').write_text('\n'.join(de_srt))
    (OUT/'english.srt').write_text('\n'.join(en_srt))
    atomic(OUT/'timeline.json',dict(duration_seconds=cursor, turns=[dict(start=s,end=e,**l) for s,e,l in timeline]))
    graph = ('[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,'
             'setsar=1[bg];'
             '[1:a]asplit=2[a][wave];[wave]volume=3,showwaves=s=700x100:mode=cline:rate=25:colors=0xFFE78D:scale=sqrt,format=rgba,colorkey=0x000000:0.02:0.0[w];'
             '[bg][w]overlay=610:920:shortest=1,ass=output/subtitles.ass[v]')
    run('ffmpeg','-v','error','-y','-loop','1','-framerate','25','-i',image,'-i',OUT/'podcast.wav',
        '-filter_complex',graph,'-map','[v]','-map','[a]','-c:v','libx264','-preset','fast','-crf','21',
        '-pix_fmt','yuv420p','-c:a','aac','-b:a','160k','-t',str(cursor),'-movflags','+faststart',OUT/'podcast.mp4')
    run('ffmpeg','-v','error','-y','-ss','1','-i',OUT/'podcast.mp4','-frames:v','1',OUT/'preview.png')
    print(f'Rendered {cursor:.1f} seconds')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--script', default='input/sample.json')
    parser.add_argument('--mode', choices=['plan','generate','render','mock'],default='plan')
    parser.add_argument('--max-credits',type=int,default=None)
    args=parser.parse_args()
    episode=load_episode(args.script)
    config=json.loads(Path('config.json').read_text())
    if args.max_credits is not None:
        config['max_credits']=args.max_credits
    if config['credit_ceiling_per_character'] < 1:
        raise ValueError('Conservative credit ceiling must be at least 1')
    voices=dict(male=config.get('male_voice_id') or 'AUTO_MALE',female=config.get('female_voice_id') or 'AUTO_FEMALE')
    saved = CACHE / 'voice-selection.json'
    if saved.exists():
        selection = json.loads(saved.read_text())
        voices = {g: config.get(g+'_voice_id') or selection[g] for g in ('male','female')}
    accounts=None
    if args.mode!='plan':
        if not Path(config['image']).is_file():
            raise FileNotFoundError(config['image'])
        subprocess.run(['ffmpeg','-version'], check=True, stdout=subprocess.DEVNULL)
    if args.mode=='generate':
        accounts, voices=discover(keys(),config)
    units = [[dict(line,de=line['de'][a:b]) for a,b in sentences(line['de'])] if args.mode=='mock' else generation_units(line,config,voices) for line in episode['lines']]
    flat = [unit for turn in units for unit in turn]
    report = plan(flat,config,voices,accounts)
    if args.mode=='plan':
        return
    if args.mode=='render' and report['uncached_unique_characters']:
        raise RuntimeError('Render-only mode requires every audio response in cache. No API calls made. Restore the recovery artifact or choose generate explicitly.')
    clips=[]
    for i,(line,turn_units) in enumerate(zip(episode['lines'],units)):
        sources=[]
        for j,unit in enumerate(turn_units):
            audio=OUT/f'audio-{i:04}-{j:03}.mp3'
            if args.mode in ('generate','render'):
                if args.mode=='render':
                    body, voice=payload(unit,config,voices)
                    result=json.loads((CACHE/f'{cache_id(body,voice)}.json').read_text())
                else:
                    result=synthesize(unit,config,voices,accounts)
                audio.write_bytes(base64.b64decode(result['audio_base64'],validate=True))
                alignment=result.get('alignment')
            else:
                seconds=len(unit['de'].split())/1.7
                run('ffmpeg','-v','error','-y','-f','lavfi','-i',f'sine=frequency={180 if unit["speaker"]=="male" else 260}:duration={seconds}',audio)
                alignment=None
            sources.append((audio,unit['de'],alignment))
        clips.append(pace_turn(sources,i,config,line))
    render(episode,clips,config['image'])
    if accounts:
        usage=[]
        for a in accounts:
            sub=request(a['key'],'/user/subscription')
            usage.append(dict(account=a['name'],remaining=sub['character_limit']-sub['character_count']))
        atomic(OUT/'balances-after.json',usage)


if __name__=='__main__':
    main()
