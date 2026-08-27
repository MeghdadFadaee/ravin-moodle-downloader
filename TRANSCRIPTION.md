# Transcription Accuracy, Performance, and Privacy

This policy applies to contributors and AI agents changing Ravin's local Whisper
pipeline.

## Required behavior

- Preserve source media. Preprocessing must write a separate temporary file.
- Use an explicit language when it is known. `--language auto` is available when
  it is genuinely unknown.
- Keep domain prompts optional and disabled by default.
- Never commit prompts, examples, filenames, or transcript fragments copied from
  private course recordings.
- Keep raw transcripts separate from summaries and interpreted study material.
- Treat uncertain proper nouns, numbers, and technical terms as uncertain.
- Test every quality change on representative, licensed or synthetic audio.
- Include every output-affecting option in cache metadata.

## Decoding profiles

The shared implementation is in `src/ravin/transcribe.py`.

### Accurate

The default final-quality profile uses:

```python
temperature = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
beam_size = 5
best_of = 5
patience = 1.0
condition_on_previous_text = False
word_timestamps = True
hallucination_silence_threshold = 1.5
```

Disabling previous-text conditioning reduces repetition loops, timestamp drift,
and propagation of a bad window through long lectures. Word timestamps allow
Whisper to suppress likely hallucinations over long silence.

### Balanced

Balanced mode uses a smaller beam and temperature range without word timestamp
alignment. It is suitable for routine batch work when `accurate` is too slow.

### Fast

Fast mode uses deterministic greedy decoding without beam search or word
timestamps. It is intended for drafts, not final study material.

Do not silently replace `large-v3` with `turbo`. A model change is an explicit
quality trade-off.

## Prompt privacy

`--prompt` and `WHISPER_INITIAL_PROMPT` may contain expected terminology and
proper nouns. A prompt can improve spelling, but it can also bias Whisper toward
words that were not spoken.

- Keep prompts short and relevant.
- Compare prompted output with a trusted reference or an unprompted run.
- Never include passwords, tokens, cookies, or private account information.
- Never hard-code vocabulary derived from a private course.
- Store only a SHA-256 prompt fingerprint in metadata, never the prompt text.

Neutral example:

```bash
ravin transcribe 44 --prompt "Expected networking terms and protocol names"
```

## Device and performance rules

- `--device auto` prefers CUDA, then Apple Metal, then CPU.
- FP16 is enabled on CUDA and MPS, and disabled on CPU.
- The model must be loaded once per run and reused for all pending media.
- `--threads` and `WHISPER_THREADS` are optional CPU tuning controls. Benchmark
  them because higher values are not always faster.
- MPS fallback is enabled before importing PyTorch so unsupported operations can
  run on CPU instead of failing the complete transcription.
- Completed matching outputs must remain resumable and must not reload Whisper.

## Source inspection and preprocessing

Before adding filters, inspect the media:

```bash
ffprobe -v error \
  -show_entries format=duration,size,bit_rate:stream=codec_name,sample_rate,channels \
  -of default=noprint_wrappers=1 AUDIO

ffmpeg -hide_banner -i AUDIO -af volumedetect -f null -

ffmpeg -hide_banner -i AUDIO \
  -af silencedetect=noise=-38dB:d=0.45 -f null -
```

Repeated peaks at `0.0 dB` can indicate clipping. Denoising, normalization,
frequency filtering, silence removal, and VAD must be validated by comparison;
blind preprocessing can remove quiet speech and word boundaries.

## Cache and metadata

Transcript cache matching must include:

- Source size and modification time
- Model and language
- Transcription settings schema version
- Accuracy profile
- Whether a prompt was set
- Prompt SHA-256 fingerprint

Changing any of these values must regenerate the transcript. Runtime state may
remain private under `.ravin/transcribe/`, while public artifact metadata must be
portable and must not expose absolute paths or raw prompts.

## Validation checklist

Before handing off a transcription change, verify:

- [ ] Known language selection and automatic detection both work.
- [ ] Accurate mode forwards beam, fallback, and hallucination settings.
- [ ] Fast mode does not accidentally enable beam search.
- [ ] CPU uses FP32 and accelerators use FP16.
- [ ] Prompt text is absent from metadata, state, summaries, and errors.
- [ ] A changed profile or prompt invalidates cached output.
- [ ] Empty or truncated output is handled as a result requiring review.
- [ ] Retry, interruption, atomic writes, and resume behavior still pass tests.
- [ ] Tests contain only synthetic course data and neutral terminology.
- [ ] `python3 -m unittest discover -s tests -v` passes.
- [ ] `python3 -m compileall -q src tests` passes.
