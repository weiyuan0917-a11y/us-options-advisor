; ============================================================================
;  美股期权策略推荐器 · US Options Advisor
;  Inno Setup 安装脚本（需 Inno Setup 6.3 或更高版本）
; ----------------------------------------------------------------------------
;  构建步骤：
;    1) 在项目根目录执行 PyInstaller 打包，产出 dist\USOptionsAdvisor\
;         pyinstaller --clean --noconfirm packaging\us-options-advisor.spec
;    2) 用 Inno Setup 编译器编译本脚本：
;         ISCC.exe packaging\us-options-advisor.iss
;       或双击本文件用 Inno Setup Compiler 打开后按 F9
;    3) 安装包输出到  dist\USOptionsAdvisor-Setup-<版本>-win64.exe
;
;  说明：
;    * 简体中文语言文件 ChineseSimplified.isl 与本脚本同目录，构建自包含
;    * AppMutex 与 launcher.py 创建的单实例互斥体同名，
;      安装/卸载时能自动识别「程序正在运行」并提示关闭
; ============================================================================

#define MyAppName        "美股期权策略推荐器"
#define MyAppNameEn      "USOptionsAdvisor"
#define MyAppVersion     "1.0.0"
#define MyAppPublisher   "weiyuan0917-a11y"
#define MyAppURL         "https://github.com/weiyuan0917-a11y/us-options-advisor"
#define MyAppExeName     "USOptionsAdvisor.exe"
#define MyMutexName      "Local\USOptionsAdvisor_SingleInstance"

; 与脚本同目录的资源
#define IconFile         AddBackslash(SourcePath) + "app.ico"
#define LangFile         AddBackslash(SourcePath) + "ChineseSimplified.isl"

[Setup]
; AppId 是应用的唯一标识，升级安装依赖它保持稳定，请勿随意更改
AppId={{9C4E2B71-3A8D-4F62-B5E1-7D0C9A8E4F13}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases

; 版本资源（在「应用和功能」/文件属性中显示）
VersionInfoVersion={#MyAppVersion}.0
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} 安装程序
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoCopyright=Copyright (C) 2026 {#MyAppPublisher}

; 安装位置与程序组
DefaultDirName={autopf}\{#MyAppNameEn}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
AllowNoIcons=yes

; 输出
OutputDir=..\dist
OutputBaseFilename={#MyAppNameEn}-Setup-{#MyAppVersion}-win64
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

; 压缩：lzma2 最大压缩，97MB 的发行目录可压到 ~40MB
Compression=lzma2/max
SolidCompression=yes

; 外观
WizardStyle=modern
WizardSizePercent=110

; 权限：默认按当前用户安装（免 UAC），用户也可在向导中选择为所有用户安装
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

; 平台：仅 64 位 Windows 10 及以上
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0

; 安装/卸载时若程序正在运行则提示关闭
AppMutex={#MyMutexName}
CloseApplications=yes
RestartApplications=no

; 卸载时不删除用户数据（%LOCALAPPDATA%\USOptionsAdvisor 与 ~/.longport 凭据）
Uninstallable=yes

[Languages]
#if FileExists(LangFile)
Name: "chinesesimplified"; MessagesFile: "{#LangFile}"
#endif
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[Files]
; 整个 PyInstaller onedir 产物（exe + _internal 运行时与资源）
Source: "..\dist\USOptionsAdvisor\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Comment: "美股期权策略推荐 · 交易向导"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 清理 Python 运行时产生的字节码缓存（用户数据目录保留，避免误删凭据）
Type: filesandordirs; Name: "{app}\_internal\__pycache__"
