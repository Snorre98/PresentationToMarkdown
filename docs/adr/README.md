# Architecture Decision Records

Index of architectural decisions for PresentationToMarkdown, in
[lightweight MADR](https://adr.github.io/madr/) form.

| # | Title | Status |
| --- | --- | --- |
| [0001](0001-cli-entry-points.md) | Add `ptm` and `ptm-start` console-script entry points | Accepted |
| [0002](0002-ai-flags-to-env.md) | Expose AI capabilities as flags mapped onto env vars | Accepted |
| [0003](0003-convert-cli-gui-parity.md) | `ptm` mirrors the GUI conversion semantics | Accepted |
| [0004](0004-audio-transcription-pass.md) | Add an opt-in local audio-transcription post-pass | Accepted |
| [0005](0005-asr-engine-and-model.md) | Use mlx-whisper (large-v3-turbo / large-v3) as the ASR engine | Accepted |
| [0006](0006-diarization-isolated-server.md) | Run speaker diarization in a dedicated PyTorch server | Accepted |
| [0007](0007-transcript-to-markdown-relation.md) | Relate the transcript to the Markdown as a companion section | Accepted |
| [0008](0008-audio-enhancement.md) | Enhance lecture-hall audio before ASR and persist a cleaned FLAC | Accepted |
| [0009](0009-decouple-transcription-from-conversion.md) | Decouple transcription from conversion into a `ptm-transcribe` command | Accepted |
| [0010](0010-dereverb-voice-isolation.md) | Dereverberation (WPE) and voice isolation (SepFormer) in the audio server | Accepted |
| [0011](0011-paper-structure-llm-pass.md) | Paper-mode document-structure LLM pass with image reword | Accepted |
