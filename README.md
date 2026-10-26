## Setup (one time)

### 0. Install Firefox

Hell yeah I love Firefox

### 1. Open Terminal

Press **⌘ Space**, type **Terminal** and hit Return. You'll get a window with a blinking cursor. 

### 2. Install uv

[uv](https://docs.astral.sh/uv/) is a small tool that helps get you set up. Paste this into your terminal:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

When it's done, **fully quit Terminal (⌘ Q) and open it again**. This is part of the hazing ritual.

### 3. Install psychsearch
Now, paste this into your terminal:
```sh
uv tool install https://github.com/bl-ake/PsychInfoSearch/archive/refs/heads/main.zip
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

After a run or an analysis, the **Hasse diagram** button opens a new tab showing how your strings, blocks and the full strategy fit inside one another. Narrower searches are drawn lower, and a line joins each one to the next broader search that contains it. Each search is coloured by its number of hits. Strings that were never searched on their own are drawn hollow.

Click the **☆** beside any result to save it as a **relevant article**. Saved articles are listed at the top right and kept between sessions (in `results/relevant-articles.json`). From then on, every run shows how many of them each query finds, for example **★ 4/5**. The **What the change did** cards say if a change lost one, and **Analyze** shows which strings they depend on. Each article is checked in the database you saved it from. Checking takes a few extra searches, and the cost next to **Run** includes them.

To pick up where you left off, click **Load session…** (beside **Recent changes**), choose a session from the list, and click **Load this session**. You get back its strategy, runs, analyses and database, and anything new you do is added to that session's log.

Already have a search string, like `(math* OR algebra) AND (game* OR gamif*)`? Click **Load search string…** (under **Run**) and paste it. Each part joined by AND becomes a block (named block 1, block 2, …) and each part joined by OR becomes one of its strings. Write AND and OR in capitals. NOT isn't supported. Loading replaces the current strategy, but your earlier runs stay in the log and **Restore** brings them back.

---

## Searching PsycInfo

The free databases are handy for quick checks, but they aren't PsycInfo. For real PsycInfo results, psychsearch can use **your own logged-in EBSCO session** in a separate Firefox window. You need:

- **Firefox**, installed in your Applications folder ([download it here](https://www.mozilla.org/firefox/))
- **PsycInfo access through EBSCO**, usually through your university library

### Optional: use a different EBSCO address

Psychsearch opens UConn's PsycInfo by default, so UConn students can skip this section. If your library is different, follow these steps.

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

### Choosing a database

The **Database** dropdown at the top of the page switches between PsycInfo, PubMed and ERIC. Switching starts a fresh comparison: runs from the previous database are no longer used to work out what you gained or lost. It's disabled while a search is running.

### Starting with PsycInfo

```sh
psychsearch-ui
```

Searches don't start until you click **Run**. The first time you do, a **separate Firefox window** opens. It's separate from your normal Firefox and keeps its own logins. Log in to your library there. If EBSCO shows a **cookie banner**, answer it (any option is fine). The searches start on their own once the EBSCO search page appears. If nothing seems to be happening, look at Terminal, which will say what it's waiting for.

**Leave that Firefox window open between sessions.** Then you only log in once. Some library proxies only allow a couple of sessions per person at a time, so logging in again and again can lock you out for a while.

To see PsycInfo next to the free databases, list them all: `psychsearch-ui -b ebscohost,pubmed,openalex`


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

## Updating and uninstalling

To get the latest version:

```sh
uv tool install --reinstall https://github.com/bl-ake/PsychInfoSearch/archive/refs/heads/main.zip
```

To remove it: `uv tool uninstall psychsearch`. That leaves your psychsearch folder alone.

---

## Where your stuff goes

Everything lives in your **psychsearch folder**, `Documents/psychsearch`, which psychsearch creates the first time it runs:

- **Saved terms files**: right in the folder, unless you choose somewhere else
- **`results/ui/`**: a log of every session, one file each. You can also download it from the page's **Full log**.
- **`.env`**: your settings

When you start psychsearch with a file name, like `psychsearch-ui my-terms.txt`, it looks for that file in the psychsearch folder. (Or you can drag a file from anywhere into Terminal to type its full location.)

Prefer a different folder? Start psychsearch with `PSYCHSEARCH_HOME=~/Desktop/searches psychsearch-ui`, or use `PSYCHSEARCH_HOME=.` for whichever folder Terminal is in.