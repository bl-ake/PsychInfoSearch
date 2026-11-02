"""A local web page for changing a search strategy one string at a time and seeing what each change does.

    uv run psychsearch-ui examples/terms.txt -b ebscohost

Switch strings and blocks on and off, add and remove them, then run. Each run searches the full
strategy and each block, and, when a previous run exists, the records the change added
(``new NOT old``) and the ones it lost (``old NOT new``). Both are real searches with sample records.
A side that containment shows must be empty (adding an OR string cannot lose records) is reported
as 0 without searching. "Analyze" on a block shows what each of its strings contributes and what
each switched-off string would add (the same queries as ``--refine``). "Hasse diagram" on a run or
an analysis opens its queries, blocks, strings and every combination of blocks ordered by containment
(see ``hasse.py``) in a new tab, first searching any of those not yet searched, so every node has a count.

The star on a sampled record saves it as a relevant article, in ``results/relevant-articles.json``, which
every session shares. From then on each run and analysis also asks the database which of the saved
articles each query retrieves (by record id, so only in the database each was saved from), and shows
the proportion. Containment saves searches: an article a narrower query finds, a broader one finds too.

Every action and every result is appended to ``results/ui/<timestamp>.jsonl``. The page shows the
recent entries and, on request, the full log of this or an earlier session. Any earlier run can be
restored or used as the comparison point. An earlier session can be loaded to carry on where it left
off: its log is replayed (see ``replay``) and new entries are added to it.

The page is served on 127.0.0.1 only. Searches run one at a time through the same backends, cache
and limits as the command line. For ebscohost a session of this page counts as one run: one window,
searches 5 s apart, at most EBSCOHOST_MAX_SEARCHES in all, and a stop on anything that is not a
results page (restart the page to continue; finished searches are cached).
"""

from .log import Log, read_log, replay
from .server import Server, main
from .session import Session
from .strategy import UIError, state_from_blocks, state_from_string, terms_file_text

__all__ = ["Log", "Server", "Session", "UIError", "main", "read_log", "replay",
           "state_from_blocks", "state_from_string", "terms_file_text"]
