# Spike: the CAD engine behind a second board

**Throwaway.** This folder measures one thing: whether the CADCraft headless tool is light and
safe enough to sit behind a second JARVIS board. It changes nothing in JARVIS. Nothing here is
product code, and keeping any of it is a new request to Manj.

The design note, what was checked beforehand and Manj's decisions are in the JARVIS project doc
`claude/cad-board-spike-note.md` (27 Nov 2026). In short: Manj chose the "picture board": the
engine runs headless on the server and a second board shows the drawing.

## What runs

Only on GitHub's runners, by `.github/workflows/spike-cad-engine.yml`, and only on the branch
`spike-cad-engine`:

1. Download CADCraft 0.12.0 for Linux from the storytold/cadcraft release and check it against a
   pinned SHA-256. A different file fails the job.
2. Read the file's size and the libraries it needs, without running it.
3. Put only `cadcraft-cli` in a bare container image.
4. Run it once under trace with no network, to count connection attempts.
5. `measure.py` starts `cadcraft-cli mcp` in a locked-down container (no network, read-only file
   system, no capabilities, one writable folder) and, over MCP, draws a small fictional floor
   plan, renders it, saves DXF, SVG and PDF, and reopens the DXF.
6. The measurements go to the run's summary and to an artifact kept for 14 days.

No secrets are used and the job has read-only access to the repository.

## Pass marks (agreed by Manj, 27 Nov 2026)

| Measure | Pass mark |
|---|---|
| Container image size | 150 MB or less |
| Runs with no GPU, display or sound libraries | Yes |
| Runs with the network disabled and makes no connection attempt | Yes |
| Cold start to first MCP reply | 2 s or less |
| Simple plan drawn and rendered to PNG | 5 s or less |
| Peak memory | 500 MB or less |
| Memory when idle | Recorded |

## Not measured here

- How reliably Claude drives the commands. That needs an API key and Manj's approval for the cost.
- DWG files, and drawing quality on real drawings.
- A security review of CADCraft's code.
- Anything on the JARVIS server. This spike never touches it.

## Slice 0: can Claude draw?

`draw_test.py`, `draw_requests.json` and `.github/workflows/spike-cad-draw-test.yml` are the test
the approved design asks for before anything is built (project doc `claude/cad-board-design.md`):
ten fictional requests, each drawn through the design's loop of at most four Claude calls, with
the whole recipe replayed in a fresh engine container after every step.

- **It spends real money** when the repository secret `ANTHROPIC_API_KEY` is set: up to Rs 300,
  the cap Manj approved on 27 Nov 2026. The script stops before a call that could pass the cap.
- It runs only when `draw-test-run.txt` changes on this branch, never on ordinary edits.
- With no secret it makes no Claude call: one canned drawing goes through the loop to check the
  harness, and the summary says so.
- The key is given to one workflow step and is only ever put in the request header. The engine's
  container has no network and no environment, so the engine never sees it.
- Pass mark, set before the run: at least 8 of 10 drawings pass the script's checks and look
  right to a person, with no more than four Claude calls each. Cost and time are recorded.

## Running the script against a stand-in

`measure.py --server-cmd "<command>"` talks to any newline-delimited MCP server in place of the
container. That is how the script itself was checked before the first run.
