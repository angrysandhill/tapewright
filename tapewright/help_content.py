# SPDX-FileCopyrightText: 2026 AngrySandhill
# SPDX-License-Identifier: GPL-3.0-or-later
"""Everything the Help tab says, written for someone who rarely uses a computer.

Short sentences, everyday words, and always say where on the screen something is.

Markup inside the text:
    [Label]  something on screen: a tab, button, box or tick box. It must match the app's own
             wording exactly, and tests/test_core.py fails when one no longer exists.
    <Key>    a key on the keyboard, such as <Ctrl>.

Each topic's blocks are ("p", text) for a paragraph, ("steps", [...]) for numbered steps,
("list", [...]) for a bulleted list and ("tip", text) for a highlighted tip. "actions" are
buttons shown under the answer; HelpTab.HANDLERS says what each one does.
"""

ACTION_LABELS = {
    "mp3": "Go to the To MP3 tab",
    "mp4": "Go to the To MP4 tab",
    "settings": "Go to the Settings tab",
    "mp3_folder": "Open the folder with my MP3s",
    "mp4_folder": "Open the folder with my MP4s",
    "check_updates": "Check for updates now",
    "diagnostics": "Copy details for my helper",
    "setup": "Run setup again",
    "tools_folder": "Open the helpers folder",
    "ffmpeg_page": "Open the FFmpeg download page",
    "deno_page": "Open the deno download page",
}

# The update log's last line after a batch nobody cancelled. app.py logs it and the Help tab tells
# people to wait for it, so it is spelled once, here.
UPDATES_FINISHED = "Finished updating"

COPY = "hold down <Ctrl> and press <C>"
PASTE = "hold down <Ctrl> and press <V>"

TOPICS = [
    {
        "title": "What is Tapewright?",
        "blocks": [
            ("p", "Tapewright saves things you can keep. It takes a video from a website, such as "
                  "YouTube, and saves it on your computer as music or as a video. It can also "
                  "turn a video that is already on your computer into music or into an MP4 video."),
            ("p", "Along the top of the window are four tabs. Click a tab's name to switch to it:"),
            ("list", [
                "[To MP3] makes a sound-only file. Use it for music, talks and podcasts.",
                "[To MP4] makes a video file, with both picture and sound.",
                "[Settings] keeps the helper programs Tapewright needs up to date.",
                "[Help] is this page. Click a question on the left to read its answer.",
            ]),
            ("tip", "MP3 is a kind of sound file, and MP4 is a kind of video file. Both are very "
                    "common, so phones and computers can usually play them."),
        ],
        "actions": ["mp3", "mp4"],
    },
    {
        "title": "Setting up Tapewright",
        "blocks": [
            ("p", "Tapewright uses three free helper programs. The first time it opens, a page called "
                  "Let's get Tapewright ready shows which of them this computer already has, and installs "
                  "the rest."),
            ("steps", [
                "Click [Install them now]. If some helpers are already there, the button says "
                "[Install the missing ones] instead.",
                "Wait while the helpers download and install, one after another. The bar under each one "
                "shows how far it has got. This can take a few minutes.",
                "When the page says All set!, click [Start using Tapewright].",
            ]),
            ("list", [
                "To stop, click [Cancel]. A download that was stopped is never used, but a helper stopped "
                "while it was installing may be half-installed, so install it again before you convert.",
                "[Hide this page] brings back the tabs while the helpers go on installing. To see the page "
                "again, click the button below that says Run setup again.",
                "If a helper didn't install, the words under its name turn red and say why. Click "
                "[Try again], or click [Help with this] to read what else you can do.",
                "[Not now] skips the page. To come back to it, click the [Settings] tab, then click "
                "[Run setup again].",
                "[Show details] shows every step as it happens, which is useful when someone is helping you.",
            ]),
            ("tip", "You don't need an administrator password. When the page installs the Converter or "
                    "the YouTube helper, it downloads them from their official releases on github.com, "
                    "checks them, and keeps them in Tapewright's own folder. The Downloader is added to the "
                    "Python that runs Tapewright."),
        ],
        "actions": ["setup"],
    },
    {
        "title": "Make music (MP3) from a link",
        "blocks": [
            ("p", "A link is the address of a web page. You copy the link to the video, then paste "
                  "it into Tapewright."),
            ("steps", [
                "Open the video in your web browser, the program you use for websites, such as "
                "Chrome, Edge or Firefox.",
                "Click once on the web address in the long box at the very top of the browser. "
                "It turns blue, which means it is selected.",
                f"Copy it: {COPY}.",
                "Come back to Tapewright and click the [To MP3] tab.",
                f"Click inside the [Link or file] box, then paste the link: {PASTE}.",
                "Click [Convert to MP3].",
                "Wait while the tape winds from the left reel onto the right one. When the "
                "message under the tape starts with a tick and the word Saved, your music is ready.",
                "Click [Show in folder] to see the new file.",
            ]),
            ("tip", "On YouTube you can also click Share under the video and then Copy. That copies "
                    "the link as well."),
        ],
        "actions": ["mp3"],
    },
    {
        "title": "Save a video (MP4) from a link",
        "blocks": [
            ("p", "This works just like making music, on the [To MP4] tab."),
            ("steps", [
                "Copy the video's link from your web browser, the same way as for music.",
                "Click the [To MP4] tab.",
                f"Click inside the [Link or file] box, then paste the link: {PASTE}.",
                "Leave [Format] on Compatible. Those videos play on almost everything.",
                "Click [Convert to MP4], and wait until the message says Saved.",
            ]),
            ("tip", "[Max resolution] is how sharp the picture is. 1080p looks good on most "
                    "screens. A smaller number makes a smaller file that downloads faster."),
        ],
        "actions": ["mp4"],
    },
    {
        "title": "Convert a file on my computer",
        "blocks": [
            ("p", "You can also change a video you already have, for example one copied from your "
                  "phone, into music or into an MP4."),
            ("steps", [
                "Click the [To MP3] tab to get music, or the [To MP4] tab to get a video.",
                "Click [Browse…]. A window opens so you can find your file.",
                "On the left side of that window, click the place the file is in, such as "
                "Videos, Downloads or Desktop.",
                "Click your file once, then click the Open button.",
                "Click the Convert button, and wait until the message says Saved.",
            ]),
            ("tip", "Your original file is never changed or deleted. Tapewright makes a new file "
                    "in the folder shown in the [Save to] box."),
        ],
        "actions": ["mp3", "mp4"],
    },
    {
        "title": "Where did my file go?",
        "blocks": [
            ("p", "New files are saved in the folder shown in the [Save to] box. To begin with, "
                  "that is your Downloads folder."),
            ("list", [
                "When a conversion finishes, click [Show in folder]. A window opens with your new "
                "file already selected.",
                "To save somewhere else, click [Choose…] next to the [Save to] box and pick a "
                "folder.",
                "To find the Downloads folder yourself, open File Explorer: click the yellow "
                "folder picture at the bottom of the screen, or hold down the <Windows> key and "
                "press <E>. Then click Downloads on the left.",
            ]),
            ("tip", "Tapewright never writes over a file you already have. If you convert the same "
                    "thing twice, it either says the file is already in the folder, or gives the "
                    "new copy a number, like Song (1).mp3."),
        ],
        "actions": ["mp3_folder", "mp4_folder"],
    },
    {
        "title": "What do the choices mean?",
        "blocks": [
            ("p", "You can leave every choice as it is. This is only for when you are curious."),
            ("list", [
                "[Quality], for MP3: Best sounds best. The other choices make smaller files that "
                "sound a little worse. 128 kbps is fine for talks and podcasts.",
                "[Format], for MP4: Compatible plays on phones, TVs and almost every computer. "
                "Best quality can look a little better, but some devices cannot play it.",
                "[Max resolution], for MP4: how sharp the picture is. Higher numbers look sharper "
                "and make bigger files.",
                "[Add the thumbnail as cover art (links)]: puts the video's picture on the music "
                "file, so it shows in your music player.",
                "[Download the whole playlist when the link is one]: a playlist is a list of many "
                "videos. Tick this only if you want every video in the list. That can be a lot "
                "of files, and it can take a long time.",
            ]),
        ],
        "actions": [],
    },
    {
        "title": "How long? How do I stop it?",
        "blocks": [
            ("p", "It depends on how long the video is and how fast your internet is. A song "
                  "usually takes seconds. A long film can take many minutes."),
            ("list", [
                "The tape winds from the left reel onto the right one as Tapewright works, and "
                "it never winds back. A video's picture and sound come one after the other, and "
                "a playlist's videos one after another, so the tape can be full while Tapewright "
                "is still working. The words under the tape say what it is doing and how much "
                "time is left.",
                "The little screen beside the tape says [LOADING] while Tapewright gets ready, "
                "[PLAY] while it downloads and [REC] while it makes your file. Its numbers count "
                "how long it has been working.",
                "Sometimes the reels keep turning but the tape does not move across. That is "
                "normal: Tapewright is still working. The words under the tape say what it is "
                "doing.",
                "To stop, click [Cancel]. Anything half-finished is cleaned up, so no broken "
                "files are left behind.",
                "If you close the window while it is working, Tapewright asks you first.",
            ]),
        ],
        "actions": [],
    },
    {
        "title": "What is the red warning?",
        "blocks": [
            ("p", "Tapewright uses three helper programs to do its work: the Downloader (yt-dlp), "
                  "the Converter (FFmpeg) and the YouTube helper (deno or Node.js). Websites change "
                  "often, so the helpers need updating from time to time. The red bar means at "
                  "least one of them is out of date or missing."),
            ("steps", [
                "Click [Open Settings] on the red bar.",
                "Click [Update everything out of date].",
                "A small box says what will be updated and where it comes from. Click Yes.",
                f"Wait until the log at the bottom of the Settings tab says {UPDATES_FINISHED}, "
                "then try again.",
                "If the log says to try again later, the newest version isn't ready yet, and you "
                "can still convert. When the warning box appears, click [Convert anyway]. If that "
                "button isn't there and nothing in the box says Missing, click [Cancel], click the "
                "[Settings] tab, choose [Warn before every conversion], and start the conversion "
                "again. Anything marked Missing has to be installed first.",
            ]),
            ("tip", "If you start a conversion while something is out of date, a warning box "
                    "appears first. [Update now] is the safe choice. [Convert anyway] may still "
                    "work, but if it fails, update and try again."),
        ],
        "actions": ["settings"],
    },
    {
        "title": "A helper won't install",
        "blocks": [
            ("p", "The red words under the helper's name say what went wrong. Try these, one at a time:"),
            ("steps", [
                "Check your internet: can you open a website in your browser?",
                "Click [Try again]. If that page is gone, click the [Settings] tab, then [Run setup again].",
                "If the words say Windows stopped a program from running, this computer only allows programs "
                "that have been approved. Ask whoever looks after the computer to help.",
            ]),
            ("p", "You can also put the Converter (FFmpeg) in place yourself. It takes a few minutes:"),
            ("steps", [
                "Click the button below that says Open the FFmpeg download page. Under release builds, click "
                "ffmpeg-release-essentials.zip, and your browser downloads it.",
                "When it has downloaded, open it: click it where your browser shows the download, or "
                "double-click it in your Downloads folder. Inside is a folder. Double-click that, then "
                "double-click its bin folder.",
                "Click the button below that says Open the helpers folder, then double-click the ffmpeg "
                "folder in it.",
                "Copy ffmpeg and ffprobe from the bin folder (they may be called ffmpeg.exe and "
                f"ffprobe.exe): click one, hold down <Ctrl> and click the other, then {COPY}. Click inside "
                f"the ffmpeg folder, then {PASTE}.",
                "Come back to Tapewright and click the button below that says Check for updates now.",
            ]),
            ("p", "The YouTube helper (deno) goes in the same way. On the deno download page, download "
                  "deno-x86_64-pc-windows-msvc.zip, then copy the deno file inside it, which may be called "
                  "deno.exe, into the deno folder."),
        ],
        "actions": ["ffmpeg_page", "deno_page", "tools_folder", "check_updates"],
    },
    {
        "title": "Something went wrong",
        "blocks": [
            ("p", "The red words under the tape say what the problem was. If a conversion had "
                  "already started, the little screen beside the tape also says [ERROR]. Try "
                  "these, one at a time:"),
            ("steps", [
                "Check your internet: can you open a website in your browser?",
                "Update the helpers: click the [Settings] tab, click [Check for updates], then "
                "click [Update everything out of date] if it can be clicked.",
                "Copy the link again, straight from the box at the top of your browser. Videos "
                "that are private, removed, age-restricted or for paying members cannot be saved.",
                "For a file on your computer, check that it plays in your usual video or music "
                "player.",
                "Still stuck? Ask someone to help. Click [Copy log] on the tab where it went "
                f"wrong, then paste it into an email or message to them: {PASTE}. The second "
                "button below copies more details they may ask for.",
            ]),
        ],
        "actions": ["check_updates", "diagnostics"],
    },
    {
        "title": "My file will not play",
        "blocks": [
            ("list", [
                "Check that it finished: the message under the tape should start with a tick and "
                "say Saved.",
                "For a video, convert it again on the [To MP4] tab with [Format] set to "
                "Compatible. Some players cannot play the Best quality kind.",
                "Try a different player: right-click the file, choose Open with, and pick another "
                "program.",
            ]),
        ],
        "actions": ["mp4"],
    },
    {
        "title": "Updating Tapewright",
        "blocks": [
            ("p", "When a newer Tapewright is out, the [Settings] tab says so just above the log at its "
                  "bottom, beside an [Open the download page] button."),
            ("steps", [
                "Click the [Settings] tab.",
                "Click [Open the download page]. Your web browser opens the page with the new version.",
                "Follow the steps on that page. Your settings are kept, and if a helper needs installing "
                "again afterwards, Tapewright offers to do it.",
            ]),
            ("tip", "Tapewright asks about new versions at most once a day, when it checks for updates, so a "
                    "new version can take a day to show up here."),
        ],
        "actions": ["settings"],
    },
    {
        "title": "Copy, paste and other basics",
        "blocks": [
            ("list", [
                f"Copy: select the words first, then {COPY}.",
                f"Paste: click where the words should go, then {PASTE}.",
                "Select everything in a box: click in the box, then hold down <Ctrl> and "
                "press <A>.",
                "Right-click, which means pressing the button on the right side of the mouse, "
                "opens a small menu with Copy and Paste in it. It works in Tapewright's boxes too.",
                "Pressing <Enter> in the [Link or file] box starts the conversion, the same as "
                "clicking the Convert button.",
                "To move through long text, turn the wheel on your mouse, or drag the bar at the "
                "side.",
                "Click [Bigger] or [Smaller] at the bottom of this page to change the size of "
                "this text.",
            ]),
        ],
        "actions": [],
    },
    {
        "title": "Is it OK to download videos?",
        "blocks": [
            ("p", "Only save things you are allowed to keep: your own videos, videos whose owner "
                  "says you may download them, and things marked as free to share."),
            ("p", "Many websites' rules, YouTube's included, do not allow downloading unless the "
                  "site offers its own download button. Do not share copies of other people's "
                  "music or videos. Tapewright is a tool, and what it is used for is up to you."),
        ],
        "actions": [],
    },
]
