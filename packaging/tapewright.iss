; SPDX-FileCopyrightText: 2026 AngrySandhill
; SPDX-License-Identifier: GPL-3.0-or-later
;
; Tapewright's Windows installer. .github/workflows/release.yml compiles it with Inno Setup 6.6 or later, once
; packaging/build.py's runtime, mark and stage steps have filled ..\build\runtime and ..\build\app:
;
;   ISCC.exe /DAppVersion=0.2.0 /DPyVersion=3.14.6 packaging\tapewright.iss
;
; Paths are relative to this folder. A line ending in a space and a backslash carries on in the next line:
; Inno Setup's preprocessor joins the two before anything else reads the script. tests/test_packaging.py
; reads this file too, and holds most of its directives and entries, each against app.py, runtime.json,
; build.py or the rule AGENTS.md gives for it.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef PyVersion
  #define PyVersion "3.14.6"
#endif

[Setup]
; Windows knows the installed app by this ID. A later Setup with the same one finds the same folder and
; replaces the same Installed apps entry; a new ID would install a second Tapewright beside the first.
AppId={{C7E317BA-41A6-4ADE-ABE5-173A5DAC0058}
AppName=Tapewright
AppVersion={#AppVersion}
VersionInfoVersion={#AppVersion}
AppPublisher=AngrySandhill
AppPublisherURL=https://github.com/angrysandhill/tapewright
; Just for this user, so there is no administrator prompt, and so the private Python sits in a folder its
; user can write to: the setup page installs yt-dlp into it with pip, and with PYTHONNOUSERSITE=1, which
; Tapewright sets for this Python's children, pip has no user folder to fall back on (see AGENTS.md).
; {autopf} is then {userpf}, %LOCALAPPDATA%\Programs. PrivilegesRequiredOverridesAllowed stays unset, so
; /ALLUSERS can't move it into Program Files.
PrivilegesRequired=lowest
DefaultDirName={autopf}\Tapewright
; The folder has to be one its user can write to, and one Start menu shortcut needs no folder of its own.
DisableWelcomePage=yes
DisableDirPage=yes
DisableProgramGroupPage=yes
InfoBeforeFile=before-install.txt
; python.org's amd64 runtime. x64compatible also lets it install on Arm64 Windows 11, which runs x64
; programs, though Tapewright hasn't been tried there.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Python 3.14 supports Windows 10 and later.
MinVersion=10.0
; Tapewright's own look is dark. The dark style arrived in Inno Setup 6.6.0.
WizardStyle=modern dark
; Setup and Uninstall both look for this mutex (app.APP_MUTEX) when they start, and ask for Tapewright to be
; closed. Nothing closes it for them: CloseApplications' Restart Manager would end it from outside, which
; skips App.shutdown(), the only thing that stops the FFmpeg and yt-dlp processes Tapewright starts.
AppMutex=Tapewright.Running
CloseApplications=no
SetupIconFile=tapewright.ico
UninstallDisplayIcon={app}\app\tapewright.ico
OutputDir=..\dist
; No version in the name, so /releases/latest/download/TapewrightSetup.exe always finds the newest.
OutputBaseFilename=TapewrightSetup
; The runtime is mostly Python source, which Inno Setup's help says one solid stream compresses far better.
; Its cost, unpacking every earlier file to reach a later one, is why the runtime and its marker come last in
; [Files]: an update that skips them never has to unpack them.
SolidCompression=yes

[Tasks]
Name: desktopicon; Description: "Put a Tapewright shortcut on the desktop"

[InstallDelete]
; The app folder is replaced whole every time, so a module a newer version dropped can't linger.
Type: filesandordirs; Name: "{app}\app"
; The runtime folder also holds the yt-dlp pip put there, so it is replaced only when the Python pin
; changes. Tapewright's setup page then offers yt-dlp again.
Type: filesandordirs; Name: "{app}\runtime"; Check: RuntimeChanged

[Files]
Source: "..\build\app\*"; DestDir: "{app}\app"; Flags: recursesubdirs createallsubdirs ignoreversion
; The marker is the last file written, on its own, so a Setup that stops partway through the runtime (a
; crash, a forced restart) leaves no marker, and the next Setup replaces the runtime instead of keeping half
; of one.
Source: "..\build\runtime\*"; DestDir: "{app}\runtime"; Check: RuntimeChanged; \
  Excludes: "\tapewright-runtime.txt"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\build\runtime\tapewright-runtime.txt"; DestDir: "{app}\runtime"; Check: RuntimeChanged; \
  Flags: ignoreversion

[Icons]
; -I isolates Python: no PYTHON* variable another program left behind, and no user site-packages, can break
; the start, while the runtime's own site-packages, where yt-dlp is, still loads. -I also leaves the script's
; folder off sys.path, which Tapewright.pyw puts back itself. The AppUserModelID is app.APP_USER_MODEL_ID, so
; the taskbar groups the open window with these shortcuts.
Name: "{autoprograms}\Tapewright"; Filename: "{app}\runtime\pythonw.exe"; \
  Parameters: "-I ""{app}\app\Tapewright.pyw"""; WorkingDir: "{app}\app"; \
  IconFilename: "{app}\app\tapewright.ico"; AppUserModelID: "AngrySandhill.Tapewright"
Name: "{autodesktop}\Tapewright"; Filename: "{app}\runtime\pythonw.exe"; \
  Parameters: "-I ""{app}\app\Tapewright.pyw"""; WorkingDir: "{app}\app"; \
  IconFilename: "{app}\app\tapewright.ico"; AppUserModelID: "AngrySandhill.Tapewright"; Tasks: desktopicon

[Run]
Filename: "{app}\runtime\pythonw.exe"; Parameters: "-I ""{app}\app\Tapewright.pyw"""; \
  WorkingDir: "{app}\app"; Description: "Open Tapewright now"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
; Uninstall removes what Setup copied, then these, which Tapewright added afterwards: the packages pip put in
; its Python, the bytecode Python wrote beside the app's modules, and the helpers the setup page downloaded.
; A folder that was still full when Uninstall first tried it, {app} among them, is tried again once
; Uninstall's own files are gone. Settings, in %APPDATA%\Tapewright, are kept.
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{localappdata}\Tapewright\tools"
Type: dirifempty; Name: "{localappdata}\Tapewright"

[Code]
var
  RuntimeChecked: Boolean;
  RuntimeIsChanged: Boolean;

// True when {app}\runtime isn't the Python this Setup carries: no python.exe, no marker, or a marker naming
// another version. Setup may ask several times (on the Preparing page when Windows has renames pending, for
// the progress bar's size, for [InstallDelete], then for each runtime file), the first always after {app} is
// known and before the folder changes. The first answer, which describes the folder as it was, is kept, so
// every call in one Setup agrees whatever has been deleted or copied by then.
function RuntimeChanged(): Boolean;
var
  Runtime: String;
  Marker: AnsiString;
begin
  if not RuntimeChecked then begin
    Runtime := ExpandConstant('{app}\runtime\');
    RuntimeIsChanged := not FileExists(Runtime + 'python.exe')
      or not LoadStringFromFile(Runtime + 'tapewright-runtime.txt', Marker)
      or (Trim(Marker) <> '{#PyVersion}');
    RuntimeChecked := True;
  end;
  Result := RuntimeIsChanged;
end;

// AppMutex is looked for only when Setup starts, and Tapewright can be opened while the wizard is up. This
// runs after the last page and before anything is deleted, so it asks again, in Setup's own words. Cancel,
// or a silent install run with /SUPPRESSMSGBOXES, stops Setup with nothing changed.
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  while CheckForMutexes('Tapewright.Running') do
    if SuppressibleMsgBox(FmtMessage(SetupMessage(msgSetupAppRunningError), ['Tapewright']), mbError,
      MB_OKCANCEL, IDCANCEL) <> IDOK then begin
      Result := 'Tapewright is still open.';
      Exit;
    end;
end;
