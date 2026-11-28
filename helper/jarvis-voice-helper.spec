# PyInstaller spec: one-folder console build of the JARVIS voice helper.
#   uv run pyinstaller jarvis-voice-helper.spec --noconfirm
# Output: dist/jarvis-voice-helper/ (jarvis-voice-helper.exe on Windows).
# The Kokoro model files are NOT bundled: the app downloads them.
import importlib.util

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_dynamic_libs

datas, binaries, hiddenimports = [], [], []


def add(name):
    d, b, h = collect_all(name)
    datas.extend(d)
    binaries.extend(b)
    hiddenimports.extend(h)


# espeak-ng library and its data folder, kokoro-onnx data files, onnxruntime native libraries,
# Pipecat's package data (and its lazily imported service modules).
for package in ("espeakng_loader", "kokoro_onnx", "onnxruntime", "pipecat"):
    add(package)
# PyAudio's native library, only when PyAudio is installed (optional "audio" extra).
if importlib.util.find_spec("pyaudio") is not None:
    add("pyaudio")

a = Analysis(
    ["jarvis_voice_helper/__main__.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="jarvis-voice-helper",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="jarvis-voice-helper",
)
