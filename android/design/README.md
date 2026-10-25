# Book Listen design / v0.3

Reference: reference.png, generated with the built-in image generator. Prompt: professional Android audiobook Library and Read-along screens, charcoal #101412, emerald #78EFA3, editorial typography, real book covers, persistent import progress and readable highlighted narration. The reference is a concept, not a screenshot of the app.

Implementation uses dark surfaces, off-white primary text, emerald primary actions, serif wordmark and narration, native system sans-serif controls, 16dp rounded cards, 20dp screen gutters and 52dp minimum buttons. Covers come from audiobook metadata; missing covers use title typography. The native adaptive icon uses a two-page mint book silhouette with a play cutout and dark circular backing.

Read-along follows timed synthesized passages (a passage can contain multiple grouped paragraphs). It does not claim word-level alignment. Timing is derived from synthesized sample lengths, including silence and chapter offsets. The M4B carries a versioned UUID box, compatible with ordinary players that ignore unknown boxes. Existing books may attach a matching non-overlapping WebVTT file. Generating or transcribing arbitrary existing audio is not included.
