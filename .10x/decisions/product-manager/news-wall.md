# Product Manager — news-wall (ACCEPTED 2026-11-21)

Source spec: `.10x/specs/2026-11-21-jarvis-news-wall-design.md`

## User
Dr. Manjunath Haridas — single user, Windows PC, Indian English speaker.

## User stories
1. As the user, I tap the screen or say "Hey Jarvis" and JARVIS greets me aloud, so I know it's listening.
2. As the user, I say "bring me the latest news" (or any rewording) and a wall appears: Indian live TV + Indian headlines (main), international headlines (side).
3. As the user, I say "open number 4" / "tell me more about number 2" and JARVIS expands or summarises that story.
4. As the user, I say "switch channel" / "close the video" / "clear the screen" and the screen changes accordingly.
5. As the user, I can follow up within ~10 s without repeating "Hey Jarvis".
6. As the user, I say "Jarvis, stop listening" and the mic is off until I tap.

## Acceptance criteria
- Stories 1–6 work end to end on the user's Windows PC.
- ≤3 s from end of speech to panels appearing (normal connection), measured.
- Brain picks the correct action for ≥90% of the evaluation phrasings.
- Raw audio never leaves the PC (verified by code review + network check).
- Failure cases in spec §4 behave as specified.

## Out of scope
Calendar, Gmail, social, PC/app control, cloud voice, local brain, shell replacement at boot.

## Open item
Voice: default = natural local open-source voice (user approved spec with this default).
