; 玲珑剪切板 Windows 安装向导
;
; 为什么是 NSIS 而不是 zip：zip 解出来的 exe 是「裸奔」的 ——
; 没有开始菜单入口、没有添加/删除程序里的记录、没有卸载器，
; 图标缓存也不会更新（用户看到的是白纸图标，而不是我们的图）。
;
; 用 /D 从 build.py 传入的变量：
;   APP_VERSION  1.0.2
;   SOURCE_DIR   目录模式产物（dist/玲珑剪切板）
;   OUTPUT_FILE  最终 setup.exe 路径
;   ICON_FILE    .ico 路径
;
; 编译：makensis 在 Linux 上也能跑（apt-get install nsis），
; 所以 Windows 安装包和 .exe 一样可以在 Linux 构建机上产出。

Unicode true

!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"

!define APP_NAME "玲珑剪切板"
!define APP_NAME_EN "Linglong Clipboard"
!define APP_PUBLISHER "TechSauce"
; 可执行文件名是 ASCII，**界面上的中文名靠快捷方式和窗口标题**。
; 中文 exe 名在 NSIS + Wine 这条链路上会丢：编译能过，写盘时主程序直接消失，
; 装出来只剩 _internal 和卸载器 —— 这个只有「装一次再列目录」才会发现。
!define APP_EXE "linglong-clipboard.exe"
!define APP_ID "LinglongClipboard"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_ID}"

Name "${APP_NAME} ${APP_VERSION}"
BrandingText "${APP_NAME} ${APP_VERSION}"

OutFile "${OUTPUT_FILE}"
InstallDir "$LOCALAPPDATA\Programs\${APP_ID}"
InstallDirRegKey HKCU "Software\${APP_ID}" "InstallDir"
; 普通用户目录安装：不需要管理员权限，也不会触发 UAC 弹窗
RequestExecutionLevel user
SetCompressor /SOLID lzma

VIProductVersion "${APP_VERSION}.0"
VIAddVersionKey "ProductName" "${APP_NAME}"
VIAddVersionKey "FileDescription" "${APP_NAME} 安装程序"
VIAddVersionKey "FileVersion" "${APP_VERSION}"
VIAddVersionKey "ProductVersion" "${APP_VERSION}"
VIAddVersionKey "LegalCopyright" "MIT License"
VIAddVersionKey "CompanyName" "${APP_PUBLISHER}"

!define MUI_ICON "${ICON_FILE}"
!define MUI_UNICON "${ICON_FILE}"
!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "立即启动 ${APP_NAME}"
!define MUI_FINISHPAGE_SHOWREADME ""
!define MUI_FINISHPAGE_SHOWREADME_NOTCHECKED
!define MUI_WELCOMEPAGE_TITLE "安装 ${APP_NAME} ${APP_VERSION}"
!define MUI_WELCOMEPAGE_TEXT "跨平台智能剪切板：本地优先、语义检索、可编程转换。$\r$\n$\r$\n安装到当前用户目录，不需要管理员权限。$\r$\n历史数据保存在你自己的用户目录里，卸载时不会删除。"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"

Section "主程序" SecMain
  SectionIn RO
  SetOutPath "$INSTDIR"

  ; 目录模式产物：exe + Qt 运行库 + 图标资源。整个目录搬过去，
  ; 少一个 dll 就是「双击没反应」，而这种缺失在构建机上看不出来。
  File /r "${SOURCE_DIR}\*.*"

  WriteUninstaller "$INSTDIR\uninstall.exe"

  CreateDirectory "$SMPROGRAMS\${APP_NAME}"
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_EXE}" 0
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\卸载 ${APP_NAME}.lnk" "$INSTDIR\uninstall.exe"

  ; 桌面快捷方式：安装版的意义就在这里，装完能直接双击
  CreateShortCut "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_EXE}" 0

  WriteRegStr HKCU "Software\${APP_ID}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\${APP_ID}" "Version" "${APP_VERSION}"

  ; 添加/删除程序里的条目。少了它，用户想卸载只能自己去翻文件夹。
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "${APP_PUBLISHER}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\${APP_EXE}"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" "$\"$INSTDIR\uninstall.exe$\""
  WriteRegStr HKCU "${UNINST_KEY}" "QuietUninstallString" "$\"$INSTDIR\uninstall.exe$\" /S"
  WriteRegStr HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1

  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKCU "${UNINST_KEY}" "EstimatedSize" "$0"

  ; 通知 shell 刷新图标缓存与关联：不做的话开始菜单里可能还是白纸图标
  System::Call 'shell32::SHChangeNotify(i 0x08000000, i 0, i 0, i 0)'
SectionEnd

Section "Uninstall"
  ; 只删程序，**不删用户历史数据**：
  ; 卸载时清空剪贴板历史是不可接受的，用户重装后应该还在。
  Delete "$DESKTOP\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\卸载 ${APP_NAME}.lnk"
  RMDir "$SMPROGRAMS\${APP_NAME}"

  ; 递归删除安装目录（exe、dll、图标、uninstall.exe 自己）
  RMDir /r "$INSTDIR"

  DeleteRegKey HKCU "${UNINST_KEY}"
  DeleteRegKey HKCU "Software\${APP_ID}"

  System::Call 'shell32::SHChangeNotify(i 0x08000000, i 0, i 0, i 0)'
SectionEnd

Function .onInit
  ; 已经装过就沿用原目录，避免出现两份安装
  ReadRegStr $0 HKCU "Software\${APP_ID}" "InstallDir"
  ${If} $0 != ""
    StrCpy $INSTDIR $0
  ${EndIf}
FunctionEnd
