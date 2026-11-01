# German podcast — Eleven v3 dialogue

The active workflow uses `scripts/dialogue_v3.py` and `input/v3-learning-german.json`. The complete short male/female conversation is sent as **one** Text to Dialogue request with `model_id: eleven_v3`. Turns may contain multiple sentences. Delivery, emotion and pauses are guided by tags such as `[curious]`, `[thoughtful]`, `[short pause]` and `[chuckles]` inside the script.

**No inserted silence, no playback stretching, no sentence-by-sentence synthesis, no waveform, and no background panels.** Your existing `podcast.png` is used. Each caption is one authored German phrase (up to 5 words) with its matching English translation (up to 6 words), timed from the returned character alignment. The sample translations all fit within 5 words. Tags never appear in captions.

## Run

Open **Actions → German Podcast → Run workflow**. The default script is `input/v3-learning-german.json`.

- `plan`: offline credit estimate, no API calls.
- `generate`: create missing v3 dialogue audio and render the video.
- `render`: reuse the cached complete dialogue only; cannot call ElevenLabs.

Changing the default v3 input on `main` triggers generation with a 700-credit estimate budget. Code/config changes rerender the cached dialogue. The test contains 503 characters including audio tags, with a conservative estimate of 503 credits at the configured 1-credit ceiling. No Flash discount is assumed. Actual rates may vary by voice/plan. English is never synthesized.

Download the MP4, original provider MP3, SRTs, caption preview, timeline and credit report from Actions artifacts. Paid audio and alignment are also uploaded as an audio-recovery artifact and cached for retries. Cache retention is not permanent; retain recovery artifacts for important episodes.

## Script

```json
{
  "title":"Deutsch lernen",
  "level":"A1",
  "turns":[
    {"speaker":"male","text":"[curious] Wie lernst du Deutsch? Was hilft dir jeden Tag?","chunks":[
      {"de":"Wie lernst du Deutsch?","en":"How do you learn German?"},
      {"de":"Was hilft dir","en":"What helps you"},
      {"de":"jeden Tag?","en":"every day?"}
    ]},
    {"speaker":"female","text":"[thoughtful] Ich höre kurze Dialoge.","chunks":[
      {"de":"Ich höre kurze Dialoge.","en":"I listen to short dialogues."}
    ]}
  ]
}
```

The `text` is the tagged German actually sent to ElevenLabs. `chunks` are short human-translated phrase pairs. German chunks must reproduce every spoken word in order, with tags omitted. They do not affect the generated delivery or cache identity. Keep phrases meaningful rather than splitting German and English mechanically by word position. Changing captions or translations reuses the exact same audio.

Tag delivery is model-directed, not an exact pause-duration guarantee. Do not use the old `pause_after`, `pace` or `sentence_pauses` fields in v3 scripts. The active v3 renderer never uses them. For reliable generation, the current test runner enforces the documented 2,000-character limit per dialogue request. Longer future episodes need conversation blocks, not individual sentence requests.

## Keys, budget and safe retries

Existing Actions secrets `ALVIN`, `AMY_OLIVER`, `FLORINA`, `JOHN_SNOW` are supported. Add more via `ELEVENLABS_KEYS_JSON`, a secret JSON object mapping labels to API keys. Empty keys are skipped and duplicate accounts are deduplicated. Keys need user/subscription and voice read access, plus permission to generate dialogue/TTS. The same male/female voice IDs must be available on the eligible account.

The full dialogue must fit in one account. On a confirmed quota rejection the entire ungenerated request moves to the next funded account. Explicit rate-limit responses wait and retry the same account. Abuse/policy blocks and ambiguous transport/server failures do not rotate keys. A pending journal prevents uncertain paid requests from being repeated. Inspect ElevenLabs history before clearing an uncertain marker.

Each paid dialogue response is saved before any rendering or alignment work. Alignment mismatches stop subtitle rendering and preserve the paid audio for repair. Budgets are estimates; provider-side overage settings determine a hard billing cap.

## Local checks

Requires Python 3.12 and FFmpeg with libass and DejaVu Sans.

```sh
python -m unittest discover -s tests -v
python scripts/dialogue_v3.py --mode plan
python scripts/dialogue_v3.py --mode render --max-credits 0
```

The older `scripts/podcast.py` remains as shared API/cache utilities and the legacy Flash renderer. The active Actions workflow invokes only `dialogue_v3.py`.

References: [Dialogue with timestamps](https://elevenlabs.io/docs/api-reference/text-to-dialogue/convert-with-timestamps), [audio tags](https://elevenlabs.io/docs/help-center/product/core-capabilities/text-to-speech/how-do-audio-tags-work-with-eleven-v3-and-v4), [pauses](https://elevenlabs.io/docs/help-center/product/core-capabilities/text-to-speech/how-can-i-add-pauses).
