# psychsearch

Building a literature search is a lot of trial and error: add a term, rerun the search, squint at the hit count, repeat. psychsearch gives you a page in your web browser where you can switch search terms on and off, hit **Run**, and see right away:

- how many hits your search gets
- a sample of the records it finds, so you can check they're on topic
- exactly which records your last change **gained and lost**
- which of your terms are pulling their weight, and which add nothing at all
- your search written in PsycInfo syntax, ready to paste into EBSCO

It can search **APA PsycInfo** (through your library's EBSCO login) plus several free databases: **PubMed**, **OpenAlex** and **Europe PMC**.

This guide assumes a Mac and no programming experience. You'll type a handful of commands into the Terminal app to get set up. You can copy and paste every one of them.

---

## Setup (one time)

### 1. Open Terminal

Press **⌘ Space**, type **Terminal** and hit Return. You'll get a window with a blinking cursor. That's where the commands below go: paste one in and press Return.

### 2. Install uv

[uv](https://docs.astral.sh/uv/) is a small tool that installs psychsearch, along with the right version of Python and everything else it needs. Paste this in:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

When it's done, **quit Terminal (⌘ Q) and open it again** so it can find `uv`.

### 3. Install psychsearch

```sh
uv tool install https://github.com/YOUR-NAME/PsychInfoSearch/archive/refs/heads/main.zip
```

This takes a minute the first time. When it says `Installed 3 executables`, you're done.

---

## Starting psychsearch

Each time, open Terminal and run:

```sh
psychsearch-ui
```

The page opens in your web browser. **Keep the Terminal window open while you work.** When you're done, click on Terminal and press **Ctrl C** to stop.

That starts you with the free databases (PubMed and OpenAlex). To search PsycInfo, see [Searching PsycInfo](#searching-psycinfo) below.

---

## Using the page

**Build your strategy.** Your search is made of **blocks**, one per concept (say, *math*, *games* and *students*):

- The terms **inside** a block are joined with OR: `math*` OR `mathematics`
- The blocks are joined **to each other** with AND: *(math) AND (games) AND (students)*

Use **Add block** to make a block, then type terms into it and click **Add**.

**Switch things on and off.** Click a term to switch it off, without deleting it, and click again to bring it back. Each block has its own on/off switch too. To delete something for good, click its **×**. That's the whole idea: try a term, see what it does, and keep it or don't.

**Run.** Click **Run** to search. You'll see the hit count for the full strategy and for each block, plus a sample of records with their abstracts. From the second run on, **What the change did** shows the records your edit *added* and the ones it *lost* since the previous run. These are actual records, not just a difference in numbers, so you can judge whether a change helped.

**Analyze a block.** **Analyze** on a block tests each of its terms. It shows how many hits you'd lose by switching each term off, and how many each switched-off term would add if you switched it on. The button shows how many searches it'll take before you click.

**Spot dead weight.** Terms that add nothing get flagged right away, with no searching needed. For example, `mathematics` adds nothing if you already have `math*`.

**Go back in time.** Every change and every run is logged. Open any earlier run and click **Restore this strategy** to go back to it, or **Compare next runs with this** to measure your changes against it instead of the previous run. **Full log** shows the whole history, including earlier sessions.

**Save your work.** **Save as terms file…** writes your strategy to a text file in your psychsearch folder (`Documents/psychsearch`). To pick up where you left off next time, start psychsearch with that file:

```sh
psychsearch-ui terms-ui.txt
```

### How to write search terms

- **`AND`, `OR`, `NOT` must be in capitals.** A lowercase "and" is just a word.
- **`*` truncates:** `adolescen*` finds adolescent, adolescents, adolescence…
- **Phrases:** `"serious game"` is a phrase. So is `serious game` without quotes: words typed together count as a phrase unless you put an operator between them.
- **Parentheses** group, as usual: `(teen* OR adolescen*) NOT adult*`

---

## Searching PsycInfo

The free databases are handy for quick checks, but they aren't PsycInfo. For real PsycInfo results, psychsearch can use **your own logged-in EBSCO session** in a separate Firefox window. You need:

- **Firefox**, installed in your Applications folder ([download it here](https://www.mozilla.org/firefox/))
- **PsycInfo access through EBSCO**, usually through your university library

### One-time setup: tell psychsearch where your EBSCO lives

1. In your normal browser, log in to PsycInfo through your library the way you usually do and run any search. Copy the address from the address bar. It'll look something like
   `https://research-ebsco-com.ezproxy.yourschool.edu/c/abc123/search?...`

2. In Terminal, make a settings file in your psychsearch folder and open it in TextEdit:

   ```sh
   mkdir -p ~/Documents/psychsearch && touch ~/Documents/psychsearch/.env && open -e ~/Documents/psychsearch/.env
   ```

3. Type `EBSCOHOST_URL=` and paste your address right after it, all on one line, like this:

   ```text
   EBSCOHOST_URL=https://research-ebsco-com.ezproxy.yourschool.edu/c/abc123/search
   ```

   Save and close. (Files whose names start with a dot are hidden in Finder. Press **⌘ Shift .** if you want to see this one.)

### Starting with PsycInfo

```sh
psychsearch-ui -b ebscohost
```

Searches don't start until you click **Run**. The first time you do, a **separate Firefox window** opens. It's separate from your normal Firefox and keeps its own logins. Log in to your library there. If EBSCO shows a **cookie banner**, answer it (any option is fine). The searches start on their own once the EBSCO search page appears. If nothing seems to be happening, look at Terminal, which will say what it's waiting for.

**Leave that Firefox window open between sessions.** Then you only log in once. Some library proxies only allow a couple of sessions per person at a time, so logging in again and again can lock you out for a while.

To see PsycInfo next to the free databases, list them all: `psychsearch-ui -b ebscohost,pubmed,openalex`

### Please go easy on it

EBSCO's terms don't love automated searching, so psychsearch deliberately behaves like a patient human: one visible window, a 5-second pause between searches, at most 120 searches per session, and results pages only (it never opens full text). If anything unexpected shows up, like a login page or a CAPTCHA, it **stops** rather than trying to get around it. Searches it has already done are remembered for 7 days, so restarting psychsearch and clicking **Run** again picks up where it left off without re-searching.

---

## Start-up options

Add these after `psychsearch-ui` when you start it. They apply to the whole session, and you can combine them: `psychsearch-ui my-terms.txt -b ebscohost --years 2010-2025`

| Option | What it does |
|---|---|
| `my-terms.txt` | Start from a saved terms file instead of a blank page |
| `-b ebscohost` | Which databases to search: `ebscohost` (PsycInfo), `pubmed`, `openalex`, `europepmc`. List several with commas. Default is `pubmed,openalex` |
| `--years 2010-2025` | Only papers published in those years |
| `--field title` | Search titles only. Other choices: `tiab` (title + abstract, the default) or `all` |
| `--known known.txt` | A text file of DOIs or PubMed IDs for papers you *know* belong in your results, one per line. Each run reports how many of them your search finds, and which ones it misses |
| `--sample 25` | How many sample records to show per search (default 10) |

---

## Writing a terms file by hand

If you'd rather type out a strategy than click it together, make a plain-text file in your psychsearch folder (`Documents/psychsearch`). TextEdit works, but go to **Format → Make Plain Text** before saving.

```text
# Lines starting with # are notes and are ignored.

[math]
math*
mathematics

[games]
game*
? gamifi*

[students]
student*
"middle school"
```

`[name]` starts a block, with one term per line beneath it. A `? ` in front of a term means "start with this one switched off". Start psychsearch with the file: `psychsearch-ui my-terms.txt`

---

## Where your stuff goes

Everything lives in your **psychsearch folder**, `Documents/psychsearch`, which psychsearch creates the first time it runs:

- **Saved terms files**: right in the folder, unless you choose somewhere else
- **`results/ui/`**: a log of every session, one file each. You can also download it from the page's **Full log**.
- **`.env`**: your settings

When you start psychsearch with a file name, like `psychsearch-ui my-terms.txt`, it looks for that file in the psychsearch folder. (Or you can drag a file from anywhere into Terminal to type its full location.)

Prefer a different folder? Start psychsearch with `PSYCHSEARCH_HOME=~/Desktop/searches psychsearch-ui`, or use `PSYCHSEARCH_HOME=.` for whichever folder Terminal is in.

---

## Updating and uninstalling

To get the latest version:

```sh
uv tool install --reinstall https://github.com/YOUR-NAME/PsychInfoSearch/archive/refs/heads/main.zip
```

To remove it: `uv tool uninstall psychsearch`. That leaves your psychsearch folder alone.

---

## Troubleshooting

**`command not found: uv` or `command not found: psychsearch-ui`**: Quit and reopen Terminal. If that doesn't fix it, run `uv tool update-shell`, then quit and reopen Terminal again.

**"needs EBSCOHOST_URL"**: psychsearch can't find your settings. Make sure `.env` is in `Documents/psychsearch` (not somewhere else) and has the `EBSCOHOST_URL=` line. See [the setup](#one-time-setup-tell-psychsearch-where-your-ebsco-lives).

**The Firefox window opens, but nothing happens**: Check Terminal, which says what it's waiting for. Usually it's a login or the cookie banner. It waits up to 15 minutes.

**"Your account is limited to 2 sessions"** (or similar): Your library proxy thinks you're logged in too many times. Close any extra windows, wait a while (often an hour or so), and try again with just one window.

**"stopped at the limit of 120 searches"**: That's the safety cap. Restart psychsearch (Ctrl C, then `psychsearch-ui -b ebscohost` again) and carry on. To change the cap, add a line like `EBSCOHOST_MAX_SEARCHES=200` to `.env`.

**OpenAlex says the daily allowance ran out**: Without a key, OpenAlex allows about 100 searches a day. Get a free key from [openalex.org](https://openalex.org) and add a line like `OPENALEX_API_KEY=yourkey` to `.env`.

**"ports … are all in use"**: psychsearch is probably already running in another Terminal window. Use that one, or stop it with Ctrl C.

---

## For command-line fans

The same install also gives you `psychsearch`, which runs whole batches of term combinations in one go and writes reports (Markdown, CSV and JSON) to `results/` in your psychsearch folder. It also gives you `psychsearch-hasse`, which draws a diagram of how those searches nest inside each other. Run `psychsearch --help` to see what they can do.
