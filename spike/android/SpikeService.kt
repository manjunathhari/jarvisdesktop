package __PACKAGE__

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.ServiceInfo
import android.os.BatteryManager
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

// THROWAWAY (slice 0 spike). Keeps the app's process, and so its microphone and Rust core,
// alive with the screen locked, and writes the battery level every five minutes.
class SpikeService : Service() {
  private var wakeLock: PowerManager.WakeLock? = null
  private val handler = Handler(Looper.getMainLooper())
  private val tick = object : Runnable {
    override fun run() {
      logBattery()
      handler.postDelayed(this, 5 * 60 * 1000L)
    }
  }

  override fun onBind(intent: Intent?): IBinder? = null

  override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
    val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
    val notification: Notification = if (Build.VERSION.SDK_INT >= 26) {
      manager.createNotificationChannel(
        NotificationChannel("jarvis", "JARVIS listening", NotificationManager.IMPORTANCE_LOW)
      )
      Notification.Builder(this, "jarvis")
    } else {
      @Suppress("DEPRECATION")
      Notification.Builder(this)
    }
      .setContentTitle("JARVIS is listening")
      .setContentText("Spike build: say \"Hey Jarvis\"")
      .setSmallIcon(android.R.drawable.ic_btn_speak_now)
      .setOngoing(true)
      .build()
    if (Build.VERSION.SDK_INT >= 30) {
      startForeground(1, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
    } else {
      startForeground(1, notification)
    }
    if (wakeLock == null) {
      val power = getSystemService(Context.POWER_SERVICE) as PowerManager
      wakeLock = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "jarvis:spike").apply { acquire() }
    }
    handler.removeCallbacks(tick)
    handler.post(tick)
    return START_STICKY
  }

  override fun onDestroy() {
    handler.removeCallbacks(tick)
    wakeLock?.let { if (it.isHeld) it.release() }
    wakeLock = null
    super.onDestroy()
  }

  private fun logBattery() {
    try {
      val status = registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
      val level = status?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1
      val scale = status?.getIntExtra(BatteryManager.EXTRA_SCALE, 100) ?: 100
      val plugged = (status?.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) ?: 0) != 0
      val percent = if (scale > 0) level * 100 / scale else level
      val time = SimpleDateFormat("HH:mm", Locale.US).format(Date())
      File(applicationInfo.dataDir, "spike-battery.log")
        .appendText("$time  $percent%${if (plugged) "  (charging)" else ""}\n")
    } catch (e: Exception) {
      android.util.Log.w("jarvis-spike", "battery log failed", e)
    }
  }
}
