<#
.SYNOPSIS
  Start JARVIS at login and add it to the Start menu (so it can be pinned to the taskbar).
.DESCRIPTION
  User level only: two shortcuts in YOUR folders. No admin rights, no service,
  no scheduled task, no registry changes, no firewall rules.

    Autostart:  shell:startup\JARVIS.lnk   -> pythonw.exe -m jarvis --background --startup
    Start menu: <Programs>\JARVIS.lnk      -> pythonw.exe -m jarvis --background

  Both use assets\jarvis.ico and the AppUserModelID "JarvisHome.JARVIS", the same
  ID the JARVIS window sets, so the pinned icon and the open window are ONE
  taskbar button. Starting JARVIS again while it runs only shows its window.
.EXAMPLE
  .\scripts\autostart.ps1 -Install
  .\scripts\autostart.ps1 -Status
  .\scripts\autostart.ps1 -Uninstall
#>
param([switch]$Install, [switch]$Uninstall, [switch]$Status)
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$PythonW = Join-Path $Root '.venv\Scripts\pythonw.exe'
$Icon = Join-Path $Root 'assets\jarvis.ico'
$AppId = 'JarvisHome.JARVIS'
$StartupLnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'JARVIS.lnk'
$MenuLnk = Join-Path ([Environment]::GetFolderPath('Programs')) 'JARVIS.lnk'

# Creating a .lnk with an AppUserModelID needs the shell's COM interfaces; this small
# C# helper is compiled in memory for this PowerShell session only.
$code = @'
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;

namespace JarvisShortcut {
  [ComImport, Guid("000214F9-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IShellLinkW {
    void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszFile, int cch, IntPtr pfd, uint fFlags);
    void GetIDList(out IntPtr ppidl);
    void SetIDList(IntPtr pidl);
    void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszName, int cch);
    void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string pszName);
    void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszDir, int cch);
    void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string pszDir);
    void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszArgs, int cch);
    void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string pszArgs);
    void GetHotkey(out short pwHotkey);
    void SetHotkey(short wHotkey);
    void GetShowCmd(out int piShowCmd);
    void SetShowCmd(int iShowCmd);
    void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszIconPath, int cch, out int piIcon);
    void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string pszIconPath, int iIcon);
    void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string pszPathRel, uint dwReserved);
    void Resolve(IntPtr hwnd, uint fFlags);
    void SetPath([MarshalAs(UnmanagedType.LPWStr)] string pszFile);
  }

  [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IPropertyStore {
    void GetCount(out uint cProps);
    void GetAt(uint iProp, out PropertyKey pkey);
    void GetValue(ref PropertyKey key, out PropVariant pv);
    void SetValue(ref PropertyKey key, ref PropVariant pv);
    void Commit();
  }

  [StructLayout(LayoutKind.Sequential, Pack = 4)]
  struct PropertyKey { public Guid fmtid; public uint pid; }

  [StructLayout(LayoutKind.Explicit)]
  struct PropVariant {
    [FieldOffset(0)] public ushort vt;
    [FieldOffset(8)] public IntPtr p;
    [FieldOffset(16)] public IntPtr p2;
  }

  [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
  class CShellLink { }

  public static class Maker {
    static PropertyKey AppIdKey() {
      // PKEY_AppUserModel_ID
      return new PropertyKey { fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), pid = 5 };
    }

    public static void Create(string lnk, string target, string args, string workdir, string icon, string appId, string description) {
      var link = (IShellLinkW)new CShellLink();
      link.SetPath(target);
      link.SetArguments(args);
      link.SetWorkingDirectory(workdir);
      link.SetIconLocation(icon, 0);
      link.SetDescription(description);
      var store = (IPropertyStore)link;
      var key = AppIdKey();
      var pv = new PropVariant { vt = 31 /* VT_LPWSTR */, p = Marshal.StringToCoTaskMemUni(appId) };
      try {
        store.SetValue(ref key, ref pv);
        store.Commit();
      } finally {
        Marshal.FreeCoTaskMem(pv.p);
      }
      ((IPersistFile)link).Save(lnk, true);
    }

    public static string ReadAppId(string lnk) {
      var link = (IShellLinkW)new CShellLink();
      ((IPersistFile)link).Load(lnk, 0);
      var key = AppIdKey();
      PropVariant pv;
      ((IPropertyStore)link).GetValue(ref key, out pv);
      return pv.vt == 31 ? Marshal.PtrToStringUni(pv.p) : null;
    }

    public static string ReadCommand(string lnk) {
      var link = (IShellLinkW)new CShellLink();
      ((IPersistFile)link).Load(lnk, 0);
      var path = new StringBuilder(1024);
      var args = new StringBuilder(1024);
      link.GetPath(path, path.Capacity, IntPtr.Zero, 0);
      link.GetArguments(args, args.Capacity);
      return path.ToString() + " " + args.ToString();
    }
  }
}
'@
if (-not ('JarvisShortcut.Maker' -as [type])) { Add-Type -TypeDefinition $code -Language CSharp }

function Show-Status {
    foreach ($item in @(@{ Name = 'Autostart (vid inloggning)'; Path = $StartupLnk }, @{ Name = 'Start-menyn'; Path = $MenuLnk })) {
        if (Test-Path $item.Path) {
            $id = [JarvisShortcut.Maker]::ReadAppId($item.Path)
            $cmd = [JarvisShortcut.Maker]::ReadCommand($item.Path)
            Write-Host ("[finns]   {0}: {1}" -f $item.Name, $item.Path) -ForegroundColor Green
            Write-Host ("          kör: {0}" -f $cmd)
            Write-Host ("          AppUserModelID: {0}" -f $id)
        } else {
            Write-Host ("[saknas]  {0}: {1}" -f $item.Name, $item.Path) -ForegroundColor Yellow
        }
    }
}

if ($Install) {
    if (-not (Test-Path $PythonW)) { throw "Hittar inte $PythonW. Kör .\scripts\setup.ps1 först." }
    if (-not (Test-Path $Icon)) { throw "Hittar inte $Icon." }
    [JarvisShortcut.Maker]::Create($StartupLnk, $PythonW, '-m jarvis --background --startup', $Root, $Icon, $AppId, 'Starta JARVIS vid inloggning')
    [JarvisShortcut.Maker]::Create($MenuLnk, $PythonW, '-m jarvis --background', $Root, $Icon, $AppId, 'JARVIS hemassistent')
    Write-Host 'JARVIS startar nu vid inloggning och finns i Start-menyn.' -ForegroundColor Green
    Show-Status
} elseif ($Uninstall) {
    foreach ($p in @($StartupLnk, $MenuLnk)) { if (Test-Path $p) { Remove-Item -LiteralPath $p -Force } }
    Write-Host 'Genvägarna är borttagna. (Ett fäst program i aktivitetsfältet tar du bort med högerklick -> Ta bort.)' -ForegroundColor Green
    Show-Status
} else {
    Show-Status
}
