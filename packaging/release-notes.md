Tapewright turns a link, or a file on your computer, into an MP3 or an MP4. This version is for Windows 10 or 11, 64-bit.

## Installing it

1. Under **Assets** below, click **TapewrightSetup.exe**. You can ignore the two files called **Source code**.
2. When it has downloaded, open it. You don't need an administrator password: Tapewright is installed just for you, in your own user folder.

If your browser says the file isn't commonly downloaded, choose **Keep**. Some browsers put Keep in a menu beside the download, behind three dots. In Microsoft Edge, click the three dots beside the download, then **Keep**, then **Show more**, then **Keep anyway**.

If Tapewright has no settings saved for you on this computer yet, Setup shows a tick box, **Check for updates when Tapewright starts**, which you can untick to stop Tapewright checking for updates each time it starts. Whether Setup shows it or not, Tapewright's Settings tab can change this at any time.

### If Windows says "Windows protected your PC"

Windows says this about a new program from a small maker that it hasn't seen many times yet, and Tapewright's installer isn't signed yet.

1. Click **More info**.
2. Check that the name beside **App** is **TapewrightSetup.exe**, perhaps with a number such as (1) if you downloaded it more than once. If it is anything else, click **Don't run**.
3. Click **Run anyway**.

If the box only offers **OK**, this computer blocks programs that aren't signed, for example with Smart App Control. Click **OK**, and [run Tapewright from source](https://github.com/angrysandhill/tapewright#running-from-source) instead.

Code signing policy: https://github.com/angrysandhill/tapewright#code-signing-policy

### If Setup says Tapewright is running

Close the Tapewright window, then click **OK**. Setup never closes Tapewright itself.

## The first time Tapewright opens

It shows a page called **Let's get Tapewright ready**, which lists the helper programs Tapewright uses: the Downloader (yt-dlp), the Converter (FFmpeg) and the YouTube helper (deno). Nothing is downloaded until you click **Install them now**. Tapewright then gets each one from the people who make it, and checks it before using it. On a slow connection this can take several minutes.

## Updating or removing it

To update, download TapewrightSetup.exe from the newest release and open it. Your settings and helpers are kept, and if a helper ever needs installing again, Tapewright offers to do it.

To remove Tapewright, open Settings in Windows, go to Apps, then Installed apps (Apps & features on Windows 10), and uninstall Tapewright there. That removes Tapewright and the helpers it downloaded, and keeps your settings.

## Checking the download

SHA256SUMS.txt holds the SHA-256 of TapewrightSetup.exe. To compare, run `Get-FileHash .\TapewrightSetup.exe` in PowerShell, in the folder you downloaded it to. Capital or small letters don't matter.
