"""THROWAWAY (slice 0 spike): add the spike's Android pieces to the project that
`tauri android init` generates. Usage: patch.py <gen/android> <dir with the arm64 sherpa-onnx .so>"""
import pathlib
import re
import shutil
import sys

gen = pathlib.Path(sys.argv[1])
libs = pathlib.Path(sys.argv[2])
here = pathlib.Path(__file__).parent
main = gen / "app" / "src" / "main"

activities = list(main.rglob("MainActivity.kt"))
assert len(activities) == 1, activities
activity = activities[0]
source = activity.read_text()
package = re.search(r"^package\s+(.+)$", source, re.M).group(1).strip()
print("package:", package)
print("--- generated MainActivity.kt ---")
print(source)

# Ask for the microphone (and notifications) on first open; once the microphone is allowed,
# start the service that keeps listening with the screen locked.
MEMBERS = '''
  private var spikeAsked = false

  override fun onResume() {
    super.onResume()
    val wanted = mutableListOf(android.Manifest.permission.RECORD_AUDIO)
    if (android.os.Build.VERSION.SDK_INT >= 33) {
      wanted.add(android.Manifest.permission.POST_NOTIFICATIONS)
    }
    val granted = android.content.pm.PackageManager.PERMISSION_GRANTED
    val missing = wanted.filter {
      androidx.core.content.ContextCompat.checkSelfPermission(this, it) != granted
    }
    if (missing.isNotEmpty() && !spikeAsked) {
      spikeAsked = true
      androidx.core.app.ActivityCompat.requestPermissions(this, missing.toTypedArray(), 4711)
      return
    }
    val mic = androidx.core.content.ContextCompat.checkSelfPermission(
      this, android.Manifest.permission.RECORD_AUDIO
    )
    if (mic == granted) {
      try {
        androidx.core.content.ContextCompat.startForegroundService(
          this, android.content.Intent(this, SpikeService::class.java)
        )
      } catch (e: Exception) {
        android.util.Log.w("jarvis-spike", "service not started", e)
      }
    }
  }
'''
if re.search(r"class\s+MainActivity\s*:\s*TauriActivity\(\)\s*\{", source):
    end = source.rstrip().rfind("}")
    source = source[:end] + MEMBERS + "}\n"
else:
    source, n = re.subn(
        r"(class\s+MainActivity\s*:\s*TauriActivity\(\))", r"\1 {" + MEMBERS + "}", source
    )
    assert n == 1
activity.write_text(source)
(activity.parent / "SpikeService.kt").write_text(
    (here / "SpikeService.kt").read_text().replace("__PACKAGE__", package)
)

manifest = main / "AndroidManifest.xml"
xml = manifest.read_text()
permissions = "\n".join(
    f'    <uses-permission android:name="android.permission.{p}" />'
    for p in (
        "RECORD_AUDIO",
        "FOREGROUND_SERVICE",
        "FOREGROUND_SERVICE_MICROPHONE",
        "POST_NOTIFICATIONS",
        "WAKE_LOCK",
    )
)
service = (
    '        <service android:name=".SpikeService" '
    'android:foregroundServiceType="microphone" android:exported="false" />'
)
assert xml.count("<application") == 1 and xml.count("</application>") == 1
xml = xml.replace("<application", permissions + "\n    <application", 1)
xml = xml.replace("</application>", service + "\n    </application>", 1)
manifest.write_text(xml)
print("--- AndroidManifest.xml ---")
print(xml)

# The sherpa-onnx libraries the Rust core links against, in their own source folder so the
# Tauri build (which manages jniLibs) never removes them.
out = main / "spikeLibs" / libs.name  # arm64-v8a or x86_64
out.mkdir(parents=True, exist_ok=True)
for name in ("libsherpa-onnx-c-api.so", "libonnxruntime.so"):
    shutil.copy(libs / name, out / name)
# The C++ runtime the audio library (oboe) needs, from the NDK.
import os
triple = {"arm64-v8a": "aarch64-linux-android", "x86_64": "x86_64-linux-android"}[libs.name]
ndk = pathlib.Path(os.environ["NDK_HOME"])
found = list(ndk.glob(f"toolchains/llvm/prebuilt/*/sysroot/usr/lib/{triple}/libc++_shared.so"))
assert len(found) == 1, found
shutil.copy(found[0], out / "libc++_shared.so")
gradle = gen / "app" / "build.gradle.kts"
gradle.write_text(
    gradle.read_text()
    + '\n// SPIKE: the sherpa-onnx libraries.\n'
    + 'android { sourceSets { getByName("main") { jniLibs.srcDir("src/main/spikeLibs") } } }\n'
)
print("--- app/build.gradle.kts ---")
print(gradle.read_text())
