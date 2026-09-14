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
}

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
                "Wait while the bar fills up. When the message under the bar starts with a tick "
                "and the word Saved, your music is ready.",
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
                "The bar, and the words under it, show how far along it is and how much time is "
                "left.",
                "Sometimes the bar slides back and forth without filling up. That is normal: "
                "Tapewright is still working, on a step it cannot measure.",
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
            ("p", "Tapewright uses a few helper programs to do its work. Their names are yt-dlp, "
                  "FFmpeg and deno. Websites change often, so the helpers need updating from time "
                  "to time. The red bar means at least one of them is out of date."),
            ("steps", [
                "Click [Open Settings] on the red bar.",
                "Click [Update everything out of date].",
                "A small box asks whether to run the update. Click Yes.",
                "Wait until the log at the bottom of the Settings tab says done, then try again.",
            ]),
            ("tip", "If you start a conversion while something is out of date, a warning box "
                    "appears first. [Update now] is the safe choice. [Convert anyway] may still "
                    "work, but if it fails, update and try again."),
        ],
        "actions": ["settings"],
    },
    {
        "title": "Something went wrong",
        "blocks": [
            ("p", "The red words under the bar say what the problem was. Try these, one at a time:"),
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
                "Check that it finished: the message under the bar should start with a tick and "
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
