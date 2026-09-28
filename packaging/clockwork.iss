; Inno Setup script for the clockwork installer (task 32's placeholder build).
;
; Packages the onedir build (dist/clockwork/, from `tools/build_exe.ps1`) -- onedir over
; onefile for the reason mainspring measured it (mainspring's task 07): onefile extracts
; to a temp directory on every launch. Compile with Inno Setup 6's ISCC.exe:
;
;   iscc packaging\clockwork.iss
;
; and the installer lands in dist\installer\. Per-user install (PrivilegesRequired=lowest)
; since an instrument PC's operator account may not have admin rights.
;
; Unlike mainspring, clockwork has no file type of its own to associate: it drives boxes
; and the acquisition console and writes UIMF files, but mainspring is the only viewer
; (CLAUDE.md's decisions of record) -- so this carries no [Registry] section at all.
;
; The [Files] wildcard below already carries the acquisition console as a second
; payload (task 52): tools/build_exe.ps1 copies packaging/console_payload/, staged by
; tools/stage_console.py, to dist/clockwork/console/ before this script ever runs, so
; nothing here names the console directly.

#define MyAppName "clockwork"
#define MyAppVersion "1.1.0rc4"
#define MyAppPublisher "University of Washington"
#define MyAppURL "https://github.com/bushgroup/clockwork"
#define MyAppExeName "clockwork.exe"
#define MyAppCliName "clockwork-cli.exe"

[Setup]
AppId={{46222F1A-B0FE-4CC3-9672-DD9BDDD3C750}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
; The same .ico the .exe carries (tools/make_icon.py), used for the wizard's own window
; and title bar. UninstallDisplayIcon points into the install rather than at a copy, so
; Apps & features shows the icon the installed program actually has.
SetupIconFile=..\src\clockwork\app\resources\clockwork.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
OutputDir=..\dist\installer
OutputBaseFilename=clockwork-{#MyAppVersion}-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; Everything a previous version installed under _internal goes first: Setup never deletes a
; file the new build does not carry, so an upgrade would keep the old numba seed and the
; old libraries beside the new ones (mainspring's lab record, task 34).
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\clockwork\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
; The two ends of the day (lab record, task 95): `warm-up` puts the standing stack on the
; boxes and leaves clockwork serve running, `stand-down` zeroes them and stops it. Run as
; the console twin under `cmd /k`, so the window stays open on what was done; the library,
; instrument document and output folder come from the window's saved settings.
Name: "{group}\Warm up"; Filename: "{cmd}"; Parameters: "/k """"{app}\{#MyAppCliName}"" warm-up"""; WorkingDir: "{app}"; IconFilename: "{app}\{#MyAppExeName}"; Comment: "Put the standing DC bias, RF and ARB range on the boxes, ramped"
Name: "{group}\Stand down"; Filename: "{cmd}"; Parameters: "/k """"{app}\{#MyAppCliName}"" stand-down"""; WorkingDir: "{app}"; IconFilename: "{app}\{#MyAppExeName}"; Comment: "Zero the boxes, lower every output, and stop clockwork serve"
Name: "{autodesktop}\Warm up"; Filename: "{cmd}"; Parameters: "/k """"{app}\{#MyAppCliName}"" warm-up"""; WorkingDir: "{app}"; IconFilename: "{app}\{#MyAppExeName}"; Comment: "Put the standing DC bias, RF and ARB range on the boxes, ramped"; Tasks: desktopicon
Name: "{autodesktop}\Stand down"; Filename: "{cmd}"; Parameters: "/k """"{app}\{#MyAppCliName}"" stand-down"""; WorkingDir: "{app}"; IconFilename: "{app}\{#MyAppExeName}"; Comment: "Zero the boxes, lower every output, and stop clockwork serve"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
