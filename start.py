"""YoGi Dub launcher - double-click panna (illa: python start.py). run.bat vendaam."""
import os, sys, shutil, subprocess, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
VENV = os.path.join(HERE, ".venv")
VPY = os.path.join(VENV, "Scripts", "python.exe") if os.name == "nt" else os.path.join(VENV, "bin", "python")


def run(cmd, **kw):
    print(">", " ".join(cmd))
    return subprocess.call(cmd, **kw)


def find_python():
    """torch 2.5.1-ku Python 3.9-3.12 venum."""
    if (3, 9) <= sys.version_info[:2] <= (3, 12):
        return [sys.executable]
    for v in ("3.11", "3.12", "3.10"):
        try:
            if subprocess.call(["py", f"-{v}", "-c", "pass"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL) == 0:
                return ["py", f"-{v}"]
        except Exception:
            pass
    return None


def main():
    if not shutil.which("ffmpeg"):
        print("[!] ffmpeg illa. PowerShell-la idhai oddunga:  winget install Gyan.FFmpeg")
        print("    Appuram ellaa window-aiyum moodi, start.py-a thirumba open pannunga.")
        return
    if not os.path.exists(".env"):
        ex = "env_example.txt"
        if os.path.exists(ex):
            shutil.copy(ex, ".env")
            print("[!] .env file uruvaakkitten. Ippo Notepad-la thirakkum - keys ezhuthi Save pannunga,")
            print("    appuram start.py-a thirumba open pannunga.")
            try:
                os.startfile(".env")
            except Exception:
                pass
        else:
            print("[!] .env illa, env_example.txt-um illa. Rendu file-um indha folder-la irukkanum.")
        return

    if not os.path.exists(VPY):
        py = find_python()
        if not py:
            print("[!] Python 3.11 (illa 3.12) venum. python.org-la 3.11 install pannunga (Add to PATH tick).")
            return
        print("Mudhal murai setup - 10-15 nimisham aagum, internet venum...")
        if run(py + ["-m", "venv", VENV]) != 0:
            return
        run([VPY, "-m", "pip", "install", "--upgrade", "pip"])
        if run([VPY, "-m", "pip", "install", "torch==2.5.1", "torchaudio==2.5.1",
                "--index-url", "https://download.pytorch.org/whl/cpu"]) != 0:
            shutil.rmtree(VENV, ignore_errors=True)
            print("[!] torch install fail. Internet check panni thirumba try pannunga.")
            return
        if run([VPY, "-m", "pip", "install", "-r", "requirements.txt"]) != 0:
            shutil.rmtree(VENV, ignore_errors=True)
            print("[!] packages install fail. Mele irukkura error-a screenshot eduthu anuppunga.")
            return

    print("\nBot start aagudhu... (indha window-a moodaadheenga)\n")
    run([VPY, "dubpro_bot.py"])


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
    input("\nEnter amukka window moodum...")
