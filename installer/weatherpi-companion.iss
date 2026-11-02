; WeatherPi Companion installer (Inno Setup 6). See docs/companion-app.md "Installer".
;
; Build it with installer\build.ps1, which runs PyInstaller first. By hand:
;   iscc installer\weatherpi-companion.iss
; Needs dist\WeatherPi Companion\ (PyInstaller) and installer\vendor\rpi-usb-gadget-driver-setup.exe
; (installer\fetch-vendor.ps1).
;
; What it does, as admin, so the app itself never needs admin:
;   * installs the app to Program Files, with Start-menu and (optional) desktop shortcuts;
;   * installs the Pi's USB (RNDIS) driver silently if it isn't in the driver store (D4);
;   * downloads Tailscale from tailscale.com (pinned version, sha256-checked) and installs it
;     silently if it's missing (D19, D25). It isn't bundled: see installer/vendor/README.md;
;   * on uninstall, keeps the owner's keys and backups unless they say otherwise.

#define AppName "WeatherPi Companion"
#define AppExe "WeatherPi Companion.exe"
#define AppSrc "..\dist\WeatherPi Companion"
#define DriverExe "rpi-usb-gadget-driver-setup.exe"
#define RepoUrl "https://github.com/BenjaminAddison/pi-weather-station"

; The version comes from the built exe (PyInstaller stamps __version__ into it).
#ifndef AppVersion
  #define AppVersion GetStringFileInfo(AppSrc + "\" + AppExe, "ProductVersion")
#endif
#if AppVersion == ""
  #error Build the app first (installer\build.ps1), or pass /DAppVersion=x.y.z
#endif

; Tailscale for Windows, pinned. Hashes from https://pkgs.tailscale.com/stable/<msi>.sha256
#define TailscaleVersion "1.104.4"
#define TailscaleSha256Amd64 "1d12cd8b1d6dbb447da8579b1a5268ae2fc050967bebfaa02e32d2bb645ef6ee"
#define TailscaleSha256Arm64 "929fa96795d7fd8c1c1c7fa9060ae487c69095a89214c47e2e6a276fc847684c"
#define TailscaleUrlBase "https://pkgs.tailscale.com/stable/"

[Setup]
; AppId identifies the install for upgrades and uninstall: never change it.
AppId={{034D62A0-C2D1-4C18-84F6-7F6F809314CD}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=WeatherPi
AppPublisherURL={#RepoUrl}
AppSupportURL={#RepoUrl}
AppUpdatesURL={#RepoUrl}/releases/latest
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern
SetupIconFile=assets\weatherpi.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
OutputDir=..\dist
OutputBaseFilename=WeatherPi-Companion-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
; close a running copy of the app before replacing its files
CloseApplications=yes
RestartApplications=no
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Put a shortcut on the desktop"
Name: "tailscale"; Description: "Download and install Tailscale (free; lets you check on your station from anywhere)"; Check: not TailscaleInstalled

[Files]
Source: "{#AppSrc}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; ui/app.py find_driver_exe() looks in <app>\drivers\ (the app re-runs it if the driver goes missing)
Source: "vendor\{#DriverExe}"; DestDir: "{app}\drivers"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}\licences"; DestName: "WeatherPi-Companion-LICENSE.txt"; Flags: ignoreversion
Source: "licences\*"; DestDir: "{app}\licences"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
; runasoriginaluser: Setup is elevated, but the app must run as the normal user
Filename: "{app}\{#AppExe}"; Description: "Open {#AppName} now"; Flags: postinstall nowait skipifsilent runasoriginaluser

[UninstallDelete]
; the flasher's downloaded SD card images (~600 MB each); they can always be downloaded again
Type: filesandordirs; Name: "{localappdata}\WeatherPi\images"

[Code]
const
  NET_CLASS_GUID = '{4d36e972-e325-11ce-bfc1-08002be10318}';
  DRIVER_INF = 'raspberrypi-rndis.inf';

var
  DownloadPage: TDownloadWizardPage;
  DriverChecked, DriverPresent: Boolean;
  TailscaleMsiReady, RestartNeeded: Boolean;

function TailscaleInstalled: Boolean;
begin
  { the same places host/tailscale.py find_cli() looks }
  Result := FileExists(ExpandConstant('{commonpf64}\Tailscale\tailscale.exe')) or
            FileExists(ExpandConstant('{commonpf32}\Tailscale\tailscale.exe'));
end;

function TailscaleMsiName: String;
begin
  if IsArm64 then
    Result := 'tailscale-setup-{#TailscaleVersion}-arm64.msi'
  else
    Result := 'tailscale-setup-{#TailscaleVersion}-amd64.msi';
end;

function TailscaleMsiSha256: String;
begin
  if IsArm64 then
    Result := '{#TailscaleSha256Arm64}'
  else
    Result := '{#TailscaleSha256Amd64}';
end;

{ Is the Pi's RNDIS driver package in the driver store? Same rule as host/driver.py:
  a raspberrypi-rndis.inf entry of the Net class. If pnputil can't be run we say no, and the
  driver setup runs anyway (installing it twice is harmless). }
function RndisDriverInstalled: Boolean;
var
  OutFile: String;
  Output: AnsiString;
  Text, Block: String;
  P, Code: Integer;
begin
  if not DriverChecked then
  begin
    DriverChecked := True;
    DriverPresent := False;
    OutFile := ExpandConstant('{tmp}\pnputil-drivers.txt');
    if Exec(ExpandConstant('{cmd}'),
            '/C ""' + ExpandConstant('{sys}\pnputil.exe') + '" /enum-drivers > "' + OutFile + '""',
            '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0) and
       LoadStringFromFile(OutFile, Output) then
    begin
      Text := Lowercase(String(Output));
      P := Pos(DRIVER_INF, Text);
      if P > 0 then
      begin
        Block := Copy(Text, P, 400);
        DriverPresent := Pos(NET_CLASS_GUID, Block) > 0;
      end;
    end;
    Log(Format('RNDIS driver in the driver store: %d (pnputil exit %d)', [Ord(DriverPresent), Code]));
  end;
  Result := DriverPresent;
end;

procedure InitializeWizard;
begin
  DownloadPage := CreateDownloadPage(SetupMessage(msgWizardPreparing),
    'Downloading Tailscale from tailscale.com...', nil);
end;

{ Fetch the Tailscale MSI before installing. A failed download isn't fatal: the app checks
  for Tailscale and offers the download link itself. }
procedure DownloadTailscale;
var
  Url: String;
begin
  Url := '{#TailscaleUrlBase}' + TailscaleMsiName;
  try
    if WizardSilent then
      DownloadTemporaryFile(Url, TailscaleMsiName, TailscaleMsiSha256, nil)
    else
    begin
      DownloadPage.Clear;
      DownloadPage.Add(Url, TailscaleMsiName, TailscaleMsiSha256);
      DownloadPage.Show;
      try
        DownloadPage.Download;
      finally
        DownloadPage.Hide;
      end;
    end;
    TailscaleMsiReady := True;
  except
    Log('Tailscale download failed: ' + GetExceptionMessage);
    SuppressibleMsgBox('Tailscale couldn''t be downloaded, so it will be skipped for now.' + #13#10#13#10 +
      'Check this computer is online. WeatherPi Companion will help you install Tailscale ' +
      'when you get to that step.', mbInformation, MB_OK, IDOK);
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = wpReady) and WizardIsTaskSelected('tailscale') and not TailscaleMsiReady then
    DownloadTailscale;
end;

procedure SetStatus(const S: String);
begin
  WizardForm.StatusLabel.Caption := S;
  WizardForm.FilenameLabel.Caption := '';
end;

procedure InstallDriver;
var
  Code: Integer;
begin
  SetStatus('Installing the USB driver for the WeatherPi...');
  if Exec(ExpandConstant('{app}\drivers\{#DriverExe}'),
          '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-', '', SW_HIDE,
          ewWaitUntilTerminated, Code) and (Code = 0) then
    Log('RNDIS driver installed')
  else
  begin
    Log(Format('RNDIS driver setup failed (exit %d)', [Code]));
    SuppressibleMsgBox('The USB driver for the WeatherPi couldn''t be installed.' + #13#10#13#10 +
      'You can carry on: WeatherPi Companion will offer to install it again when it needs it.',
      mbError, MB_OK, IDOK);
  end;
end;

procedure InstallTailscale;
var
  Code: Integer;
begin
  SetStatus('Installing Tailscale...');
  if Exec(ExpandConstant('{sys}\msiexec.exe'),
          '/i "' + ExpandConstant('{tmp}\') + TailscaleMsiName + '" /qn /norestart',
          '', SW_HIDE, ewWaitUntilTerminated, Code) and
     ((Code = 0) or (Code = 3010) or (Code = 1641)) then
  begin
    Log(Format('Tailscale installed (msiexec exit %d)', [Code]));
    if Code <> 0 then
      RestartNeeded := True;
  end
  else
  begin
    Log(Format('Tailscale install failed (msiexec exit %d)', [Code]));
    SuppressibleMsgBox('Tailscale couldn''t be installed.' + #13#10#13#10 +
      'You can carry on: WeatherPi Companion will help you install it when you get to that step.',
      mbError, MB_OK, IDOK);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    if not RndisDriverInstalled then
      InstallDriver;
    if TailscaleMsiReady then
      InstallTailscale;
  end;
end;

function NeedRestart: Boolean;
begin
  Result := RestartNeeded;
end;

{ Uninstall: the keys, recovery password and backups live in the owner's app data folder
  (secrets.py app_home). Keep them unless the owner says otherwise; silent uninstalls keep them. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;
  DataDir := ExpandConstant('{localappdata}\WeatherPi');
  if not DirExists(DataDir) then
    Exit;
  { (a line can't start with #13: the preprocessor would read it as a directive) }
  if SuppressibleMsgBox('Also delete your WeatherPi keys, recovery password and backups from this computer?' + #13#10#13#10 +
       'Choose No if you''ll use WeatherPi Companion again. Without these files, ' +
       'this computer can''t reset your WeatherPi''s password; you''d have to flash its SD card again.' + #13#10#13#10 +
       'Folder: ' + DataDir,
       mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES then
  begin
    if DelTree(DataDir, True, True, True) then
      Log('Removed ' + DataDir)
    else
      Log('Could not fully remove ' + DataDir);
  end;
end;
