; Dehli Musikk plugins — Windows installer (Inno Setup 6). SHARED across all plugins:
; per-product identity comes in via /D defines (all REQUIRED except BuildDir), matching
; the metadata CMake emits into build\dmse_plugins\<Target>.json.
;
; Prefer the driver script, which derives everything from that metadata:
;
;     powershell -ExecutionPolicy Bypass -File packaging\windows\make_installers.ps1
;
; Manual invocation (values from build\dmse_plugins\StyloPoly.json):
;
;     cmake --build build --target StyloPoly_All --config Release
;     ISCC /DMyName="StyloPoly" /DMyDir=stylopoly-plugin /DMyTarget=StyloPoly ^
;          /DMyAppGuid=XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX /DMyVersion=1.0.0 ^
;          packaging\windows\installer.iss
;
; Output: packaging\windows\build\<name>-<version>-Setup.exe
; Optionally Authenticode-sign the resulting Setup.exe (see ../PACKAGING.md).
;
; Paths below assume the top-level build dir is <repo>\build. Override with
; /DBuildDir=... if yours differs. ISCC runs relative to this .iss file.

#ifndef MyName
  #error Pass /DMyName="<PRODUCT_NAME>" (see build\dmse_plugins\<Target>.json)
#endif
#ifndef MyDir
  #error Pass /DMyDir=<plugin repo folder> (e.g. stylopoly-plugin)
#endif
#ifndef MyTarget
  #error Pass /DMyTarget=<CMake target> (e.g. StyloPoly)
#endif
#ifndef MyAppGuid
  ; Stable per-product GUID — REQUIRED. Use windowsAppGuid from the plugin's metadata
  ; json (a deterministic UUID of the bundle id). Products must NOT share a GUID, or
  ; their installers upgrade-replace each other.
  #error Pass /DMyAppGuid=<windowsAppGuid from build\dmse_plugins\<Target>.json>
#endif
#ifndef MyVersion
  #error Pass /DMyVersion=<x.y.z> (the plugin CMakeLists VERSION)
#endif
#ifndef BuildDir
  #define BuildDir "..\..\build"
#endif

#define MyPublisher "DehliMusikk"
#define ArtRelease BuildDir + "\" + MyDir + "\" + MyTarget + "_artefacts\Release"

; Sample pack: samples ship as a memory-mapped pack, NOT inside the binaries. It installs
; to {commonappdata} (C:\ProgramData) \DehliMusikk\<product>\, where the engine looks after
; the per-user dev path. WITHOUT this, a packed plugin is SILENT on a buyer's machine.
; Plugins without a pack (embedded samples) compile this section away.
#define PackSrc "..\..\" + MyDir + "\assets\samples\samples.pak"

[Setup]
AppId={{{#MyAppGuid}}
AppName={#MyName}
AppVersion={#MyVersion}
AppPublisher={#MyPublisher}
DefaultDirName={autopf}\{#MyPublisher}\{#MyName}
DefaultGroupName={#MyName}
DisableProgramGroupPage=yes
OutputDir=build
OutputBaseFilename={#MyName}-{#MyVersion}-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
WizardStyle=modern

[Types]
Name: "full";   Description: "Full installation"
Name: "custom"; Description: "Custom installation"; Flags: iscustom

[Components]
Name: "vst3";       Description: "VST3 plug-in";   Types: full custom
Name: "standalone"; Description: "Standalone app";  Types: full custom

[Files]
; VST3 is a bundle (folder) — install the whole tree to the shared VST3 location.
Source: "{#ArtRelease}\VST3\{#MyName}.vst3\*"; DestDir: "{commoncf64}\VST3\{#MyName}.vst3"; \
    Components: vst3; Flags: ignoreversion recursesubdirs createallsubdirs

; Standalone executable.
Source: "{#ArtRelease}\Standalone\{#MyName}.exe"; DestDir: "{app}"; \
    Components: standalone; Flags: ignoreversion

; Memory-mapped sample pack (see PackSrc above) — required by both components.
#if FileExists(PackSrc)
Source: "{#PackSrc}";      DestDir: "{commonappdata}\DehliMusikk\{#MyName}"; Flags: ignoreversion
Source: "{#PackSrc}.json"; DestDir: "{commonappdata}\DehliMusikk\{#MyName}"; Flags: ignoreversion
#endif

[Icons]
Name: "{group}\{#MyName}";              Filename: "{app}\{#MyName}.exe"; Components: standalone
Name: "{autodesktop}\{#MyName}";        Filename: "{app}\{#MyName}.exe"; Components: standalone; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; \
    Components: standalone; Flags: unchecked
