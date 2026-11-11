"""Створює ярлики «pipabobik» на робочому столі та в меню «Пуск».

У ярлик записується AppUserModelID панелі — тому відкрите вікно pipabobik
групується з ярликом, і його можна закріпити на панелі задач (ПКМ → «Закріпити»).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_ID = "LoganBg23.pipabobik.panel"
ICON = ROOT / "web" / "static" / "pipabobik.ico"


def pythonw() -> Path:
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return w if w.exists() else exe


def targets() -> list[Path]:
    out = []
    try:
        from win32com.shell import shell, shellcon

        out.append(Path(shell.SHGetFolderPath(0, shellcon.CSIDL_DESKTOPDIRECTORY, None, 0)))
        out.append(Path(shell.SHGetFolderPath(0, shellcon.CSIDL_PROGRAMS, None, 0)))
    except Exception:  # noqa: BLE001
        home = Path(os.environ.get("USERPROFILE", Path.home()))
        out.append(home / "Desktop")
        appdata = os.environ.get("APPDATA")
        if appdata:
            out.append(Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return out


def with_pywin32(lnk: Path) -> None:
    import pythoncom
    from win32com.propsys import propsys, pscon
    from win32com.shell import shell

    link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)
    link.SetPath(str(pythonw()))
    link.SetArguments(f'"{ROOT / "panel.pyw"}"')
    link.SetWorkingDirectory(str(ROOT))
    link.SetIconLocation(str(ICON), 0)
    link.SetDescription("pipabobik — панель керування музикою")
    store = link.QueryInterface(propsys.IID_IPropertyStore)
    store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(APP_ID))
    store.Commit()
    link.QueryInterface(pythoncom.IID_IPersistFile).Save(str(lnk), 0)


def with_powershell(lnk: Path) -> None:
    ps = (
        f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
        f"$s.TargetPath='{pythonw()}';$s.Arguments='\"{ROOT / 'panel.pyw'}\"';"
        f"$s.WorkingDirectory='{ROOT}';$s.IconLocation='{ICON}';$s.Save()"
    )
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps], check=True)


def main() -> int:
    if sys.platform != "win32":
        print("Ярлики створюються лише на Windows.")
        return 0
    ok = 0
    for folder in targets():
        if not folder.exists():
            continue
        lnk = folder / "pipabobik.lnk"
        try:
            with_pywin32(lnk)
            ok += 1
        except Exception:  # noqa: BLE001
            try:
                with_powershell(lnk)
                ok += 1
            except Exception as e:  # noqa: BLE001
                print(f"Не вдалося створити {lnk}: {e}")
    if ok:
        print("Готово! Ярлик pipabobik є на робочому столі та в меню «Пуск».")
        print("Щоб закріпити: відкрийте pipabobik → ПКМ по значку на панелі задач → «Закріпити на панелі завдань».")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
