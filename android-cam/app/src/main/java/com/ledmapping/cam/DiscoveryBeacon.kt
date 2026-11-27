package com.ledmapping.cam

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import kotlinx.coroutines.*
import org.json.JSONObject
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress

class DiscoveryBeacon(
    private val context: Context,
    private val serviceName: String,
    private val httpPort: Int,
    private val udpBeaconPort: Int = 8089,
    private val payloadProvider: () -> JSONObject
) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var nsdManager: NsdManager? = null
    private var regListener: NsdManager.RegistrationListener? = null
    @Volatile
    private var running = false

    fun start() {
        running = true
        registerMdns()
        startUdpBeacon()
    }

    fun stop() {
        running = false
        try {
            regListener?.let { nsdManager?.unregisterService(it) }
        } catch (_: Throwable) {
        }
        scope.cancel()
    }

    private fun registerMdns() {
        try {
            val mgr = context.getSystemService(Context.NSD_SERVICE) as NsdManager
            nsdManager = mgr
            val info = NsdServiceInfo().apply {
                this.serviceName = this@DiscoveryBeacon.serviceName
                this.serviceType = "_ledcam._tcp."
                this.port = httpPort
            }
            val listener = object : NsdManager.RegistrationListener {
                override fun onServiceRegistered(serviceInfo: NsdServiceInfo) {}
                override fun onRegistrationFailed(serviceInfo: NsdServiceInfo, errorCode: Int) {}
                override fun onServiceUnregistered(serviceInfo: NsdServiceInfo) {}
                override fun onUnregistrationFailed(serviceInfo: NsdServiceInfo, errorCode: Int) {}
            }
            regListener = listener
            mgr.registerService(info, NsdManager.PROTOCOL_DNS_SD, listener)
        } catch (_: Throwable) {
        }
    }

    private fun startUdpBeacon() {
        scope.launch {
            var socket: DatagramSocket? = null
            try {
                socket = DatagramSocket().apply { broadcast = true }
                val broadcastAddr = InetAddress.getByName("255.255.255.255")
                while (running) {
                    try {
                        val bytes = payloadProvider().toString().toByteArray(Charsets.UTF_8)
                        val packet = DatagramPacket(bytes, bytes.size, broadcastAddr, udpBeaconPort)
                        socket.send(packet)
                    } catch (_: Throwable) {
                    }
                    delay(2000)
                }
            } finally {
                socket?.close()
            }
        }
    }
}
