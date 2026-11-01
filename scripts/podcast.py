#!/usr/bin/env python3
"""German dialogue renderer. No paid calls in plan or mock modes."""
import argparse
import base64
import hashlib
import json
import math
import os
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
        assert i == 0 or line['speaker'] != lines[i-1]['speaker'], 'Alternate speakers'
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


def duration(path):
    return float(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration','-of','csv=p=0',str(path)]))


def ass_time(seconds):
    n = round(seconds*100)
    return f'{n//360000}:{n//6000%60:02}:{n//100%60:02}.{n%100:02}'


def escape(text):
    return text.replace('\\', '＼').replace('{', '｛').replace('}', '｝')


def render(episode, clips, image, gap, playback_speed=0.9):
    OUT.mkdir(exist_ok=True)
    timeline, cursor = [], 0.0
    for i, (line, audio) in enumerate(zip(episode['lines'],clips)):
        clip = OUT / f'turn-{i:04}.wav'
        run('ffmpeg','-v','error','-y','-i',audio,'-af',f'atempo={playback_speed},apad=pad_dur={gap}','-ar','44100','-ac','1',clip)
        length = duration(clip)
        timeline.append((cursor, cursor+length-gap, line))
        cursor += length
    (OUT/'concat.txt').write_text(''.join(f"file 'turn-{i:04}.wav'\n" for i in range(len(clips))))
    run('ffmpeg','-v','error','-y','-f','concat','-safe','0','-i',OUT/'concat.txt','-af','loudnorm=I=-18:TP=-2:LRA=7',OUT/'podcast.wav')
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
             'setsar=1,drawbox=x=100:y=25:w=1720:h=245:color=black@0.20:t=fill,'
             'drawbox=x=570:y=895:w=780:h=150:color=black@0.35:t=fill[bg];'
             '[1:a]asplit=2[a][wave];[wave]volume=3,showwaves=s=700x100:mode=cline:rate=25:colors=0xFFE78D:scale=sqrt[w];'
             '[bg][w]overlay=610:920:shortest=1,ass=output/subtitles.ass[v]')
    run('ffmpeg','-v','error','-y','-loop','1','-framerate','25','-i',image,'-i',OUT/'podcast.wav',
        '-filter_complex',graph,'-map','[v]','-map','[a]','-c:v','libx264','-preset','fast','-crf','21',
        '-pix_fmt','yuv420p','-c:a','aac','-b:a','160k','-t',str(cursor),'-movflags','+faststart',OUT/'podcast.mp4')
    run('ffmpeg','-v','error','-y','-ss','1','-i',OUT/'podcast.mp4','-frames:v','1',OUT/'preview.png')
    print(f'Rendered {cursor:.1f} seconds')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--script', default='input/sample.json')
    parser.add_argument('--mode', choices=['plan','generate','mock'],default='plan')
    parser.add_argument('--max-credits',type=int,default=None)
    args=parser.parse_args()
    episode=load_episode(args.script)
    config=json.loads(Path('config.json').read_text())
    if args.max_credits is not None:
        config['max_credits']=args.max_credits
    if config['credit_ceiling_per_character'] < 1:
        raise ValueError('Conservative credit ceiling must be at least 1')
    voices=dict(male=config.get('male_voice_id') or 'AUTO_MALE',female=config.get('female_voice_id') or 'AUTO_FEMALE')
    accounts=None
    if args.mode!='plan':
        if not Path(config['image']).is_file():
            raise FileNotFoundError(config['image'])
        subprocess.run(['ffmpeg','-version'], check=True, stdout=subprocess.DEVNULL)
    if args.mode=='generate':
        accounts, voices=discover(keys(),config)
    plan(episode['lines'],config,voices,accounts)
    if args.mode=='plan':
        return
    clips=[]
    for i,line in enumerate(episode['lines']):
        audio=OUT/f'audio-{i:04}.mp3'
        if args.mode=='generate':
            result=synthesize(line,config,voices,accounts)
            audio.write_bytes(base64.b64decode(result['audio_base64'],validate=True))
        else:
            seconds=len(line['de'].split())/1.7
            run('ffmpeg','-v','error','-y','-f','lavfi','-i',f'sine=frequency={180 if line["speaker"]=="male" else 260}:duration={seconds}',audio)
        clips.append(audio)
    render(episode,clips,config['image'],config['pause_seconds'],config.get('playback_speed',0.9))
    if accounts:
        usage=[]
        for a in accounts:
            sub=request(a['key'],'/user/subscription')
            usage.append(dict(account=a['name'],remaining=sub['character_limit']-sub['character_count']))
        atomic(OUT/'balances-after.json',usage)


if __name__=='__main__':
    main()
