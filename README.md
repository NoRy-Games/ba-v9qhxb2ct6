# Bizarre Arena data
Balance data and notes for the Bizarre Arena VRChat world.
- [`balance.json`](balance.json): the values the world uses now. Its `version` names the version, and `changelog` points to that version's changes.
- [`changelog/`](changelog): what changed in each version.
- [`wiki/`](wiki/README.md): one page per character, with portrait, the three skills with their tags and what they do, and the original values before any change.
## Changing a value
- Find the skill by its `tag` in `balance.json` and change the number.
- Every field must stay within its limits:
  - damage 0–500;
  - duration 0–600 s;
  - range 0–1000 m;
  - barrage hits 1–200.
- For a new version, update `version` and `changelog` and add the matching `changelog/vNN.md`.
- The world checks the file between rounds. If it is not valid, the queue stays closed until it is fixed.
