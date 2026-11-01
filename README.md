# German learning podcast

Two alternating ElevenLabs voices, slow German (0.90 synthesis speed plus 0.90 local playback speed), 0.75-second sentence pauses and 1.6-second speaker pauses, your `podcast.png`, German subtitles at the upper centre, English underneath, and an audio-driven waveform near the bottom. 1920×1080 MP4, MP3, SRTs, preview and credit reports are downloadable as Actions artifacts.

## Run

Changes to rendering code, config, or the workflow on `main` automatically rerender the sample from cached audio only. They never call ElevenLabs. Script edits do not automatically generate speech. For later runs open **Actions → German Podcast → Run workflow**:

1. Choose `plan` first (offline, **zero API calls**).
2. Set `script` to your input JSON path. Use `render` to change visuals and pacing with existing audio (zero API calls; fails if audio is missing).
3. Choose `generate` explicitly when new speech is needed. Set `max_credits` to a sufficient budget for a full episode.

Full episodes should contain roughly 800–1,100 German words for 8–10 minutes at this pace, depending on pauses and delivery. The script is spoken exactly as supplied; there is no paid script-writing or translation service. Supply A1/A2 German and its English translation. `level` is a metadata check, not an automatic CEFR guarantee. The included sample uses simple A1 language. Actual audio duration is measured and recorded; no looping or padding to claim a target duration.

## Input

```json
{"title":"Im Café","level":"A1","lines":[
  {"speaker":"male","de":"Hallo! Was möchtest du trinken?","en":"Hello! What would you like to drink?"},
  {"speaker":"female","de":"Ich möchte einen Tee, bitte.","en":"I would like a tea, please."}
]}
```

Alternate `male` and `female`. Keep each turn short (German max 180 characters; English max 220) so subtitles stay readable. Split longer turns into a conversational response rather than a paragraph. English is displayed only and uses no TTS credits.

## Accounts and voices

Existing Actions secrets: `ALVIN`, `AMY_OLIVER`, `FLORINA`, `JOHN_SNOW`. Empty keys are skipped. To add any number of keys later, add an **Actions secret** named `ELEVENLABS_KEYS_JSON`, containing a JSON object mapping account labels to keys. Never commit keys. Keys require subscription/user/voices read permission and Text to Speech write permission.

The live preflight fetches real balances, deduplicates keys for the same user, discovers male/female voices (preferring German labels), and prints selected voice IDs. It requires the same two voices on every eligible failover account. Pin IDs in `config.json` after listening to the sample for a consistent series. If auto-selection cannot find suitable shared voices, set the IDs explicitly. Voice accent and clarity must be judged from the sample; language support does not guarantee a native accent.

Uses `eleven_flash_v2_5`, whose published API discount is 50%. The report includes that indicative estimate and uses **1 credit per input character** as a conservative ceiling for budget and account allocation. Pricing varies by tier and premium voice multipliers; increase `credit_ceiling_per_character` if your voices cost more. Subscription balances are refreshed before each paid turn. Check `credit-plan.json` for per-account planned use and `balances-after.json` for final balances. Other concurrent usage outside this repository can change those balances.

Only confirmed quota errors cause mid-run account switching. Throttling, authentication failures, abuse/policy blocks and uncertain errors stop the run. Each whole turn must fit in one account; audio is joined without changing speaker identity. Use only accounts and quotas you are authorized to use.

## Prevent duplicate spending

Each paid response is cached by exact German text, voice, model and settings. Existing full-turn responses are reused first. New turns are synthesized one sentence per request, then joined with local silence. Splitting text does not add a per-request credit fee; tiny rounding differences and voice rates can affect totals. English/layout edits reuse audio. Actions restores/saves this cache even on failure and uploads a separate recovery artifact. Runs are serialized. A pending marker is written before each paid request: if a response is lost, the next run stops rather than risk paying again. Inspect ElevenLabs history, recover the response or confirm no charge, then remove that marker before retrying. Do not delete caches casually.

Actions caches expire/evict and are not permanent storage. Keep the recovery artifact if audio matters; download and restore `.audio-cache` before rerunning after an eviction. A hard runner crash before upload can still lose cached responses. Budgets are estimates and cannot impose a provider-side spending cap; disable overage billing in ElevenLabs for a hard account cap.

## Local checks without credits

Requires Python 3.12 and FFmpeg with libass, plus DejaVu Sans.

```sh
python -m unittest discover -s tests -v
python scripts/podcast.py --mode plan
python scripts/podcast.py --mode mock
python scripts/podcast.py --mode render --max-credits 0
```

`mock` renders synthetic tones for layout verification and never contacts ElevenLabs. `generate` uses the Actions secrets (or equivalent environment variables locally). No extra transcription is billed: subtitle timings use measured turn durations, including inserted sentence pauses. Existing multi-sentence audio is split at the saved character timestamp boundaries without dropping samples. If exact alignment is missing, rendering stops instead of regenerating speech. Text outlines provide contrast; no dark background panels are drawn behind subtitles or the waveform. Provider character alignments are retained in cache for possible word-level subtitles later.

References: [Flash model](https://elevenlabs.io/docs/overview/models), [speech with timestamps](https://elevenlabs.io/docs/api-reference/text-to-speech/convert-with-timestamps), [subscription balances](https://elevenlabs.io/docs/api-reference/user/subscription/get).
