# qPlot icon family

The canonical editable masters are in
[`src/qplot/assets`](../../../src/qplot/assets). The packaged PNG, ICO and
ICNS files are generated from those masters by
[`scripts/render_icons.py`](../../../scripts/render_icons.py).
Normal desktop setup copies the prebuilt assets.

## Visual hierarchy

| Icon | Appearance | Role |
| --- | --- | --- |
| Program | White rounded tile with a red Coulomb peak | The viewer |
| `.db` | Red folded document with a white Coulomb peak | The main database to open |
| `.db-wal` | White document, grey log lines and one red line | The write-ahead log companion |
| `.db-shm` | White document, connected grey blocks and one red outline | The shared-memory companion |

The amount of red makes the main database easier to find among its companions.
The shared peak connects the database and program, while the document outline
distinguishes files from the application. Dedicated small-size masters simplify
the artwork and omit the WAL/SHM lettering at 16 pixels.

[`icon-family`](icon-family) contains the current family exports, its overview
and a ZIP of the editable SVGs. The standalone icons in this directory and
[`db-hierarchy-proposal`](db-hierarchy-proposal) record earlier design work;
they are not the packaged artwork. The approved red-document design from that
comparison is incorporated into the canonical database masters.

## Palette and licence

The palette uses Lancaster red `#B5121B`, dark grey `#555656`, pale grey and
white, with tints for the graph axes and document folds. It acknowledges the
project's Lancaster connection. qPlot is an independent project and is not
sponsored by Lancaster University.

These icons are original qPlot vector artwork and are distributed under the
project's [MIT licence](../../../LICENSE). They do not incorporate the
QCoDeS icon or Lancaster University's logo. The icon SVGs contain editable
paths and shapes, with no linked images or font dependencies.
