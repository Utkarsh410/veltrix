; Inno Setup script for the Windows installer, Veltrix-Data-Setup-<version>.exe.
; Built by tools/build_windows.ps1, which passes the version and the folders:
;   iscc /DAppVersion=0.1.0 /DSourceDir=…\dist\Veltrix Data /DBuildDir=…\build /DOutputDir=…\dist windows_installer.iss
; Installs for the current user (no administrator needed) unless the user picks all users.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
; The AppId stays the same for every version, so a new version replaces the old one.
AppId={{05BE6F7B-850A-44EE-B2D8-3309F7E06199}
AppName=Veltrix Data
AppVersion={#AppVersion}
AppVerName=Veltrix Data {#AppVersion}
AppPublisher=Veltrix Motorsport
AppCopyright=Copyright (c) 2026 Veltrix Motorsport. All rights reserved.
DefaultDirName={autopf}\Veltrix Data
DefaultGroupName=Veltrix Data
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
LicenseFile={#BuildDir}\licences\LICENSE.txt
OutputDir={#OutputDir}
OutputBaseFilename=Veltrix-Data-Setup-{#AppVersion}
UninstallDisplayIcon={app}\Veltrix Data.exe
SetupIconFile=icons\veltrix-data.ico
UninstallDisplayName=Veltrix Data
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ChangesAssociations=yes

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#BuildDir}\licences\LICENSE.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#BuildDir}\licences\THIRD-PARTY-NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Veltrix Data"; Filename: "{app}\Veltrix Data.exe"
Name: "{group}\Licence and third-party notices"; Filename: "{app}\THIRD-PARTY-NOTICES.txt"
Name: "{group}\{cm:UninstallProgram,Veltrix Data}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Veltrix Data"; Filename: "{app}\Veltrix Data.exe"; Tasks: desktopicon

[Registry]
; "Open with" for telemetry files, without becoming their default app (as on macOS).
Root: HKA; Subkey: "Software\Classes\Applications\Veltrix Data.exe"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "Veltrix Data"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\Applications\Veltrix Data.exe\shell\open\command"; ValueType: string; ValueData: """{app}\Veltrix Data.exe"" ""%1"""
#include BuildDir + "\extensions.iss"

[Run]
Filename: "{app}\Veltrix Data.exe"; Description: "{cm:LaunchProgram,Veltrix Data}"; Flags: nowait postinstall skipifsilent
