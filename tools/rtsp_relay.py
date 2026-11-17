"""Virtual kameralarni haqiqiy RTSP (H.264) oqimga aylantiradi — Hikvision substream kabi.

    /usr/bin/python3.12 tools/rtsp_relay.py --cameras 2 --port 8554
    → rtsp://127.0.0.1:8554/Streaming/Channels/102, /202 ...

Talab (Ubuntu): gir1.2-gst-rtsp-server-1.0 python3-gi gstreamer1.0-plugins-good
gstreamer1.0-plugins-ugly. Faqat sinov uchun; ishlab chiqarishda kerak emas.
"""
import argparse

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstRtspServer", "1.0")
gi.require_version("GstRtsp", "1.0")
from gi.repository import GLib, Gst, GstRtsp, GstRtspServer  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cameras", type=int, default=2)
    ap.add_argument("--port", type=int, default=8554)
    ap.add_argument("--camsim", default="http://127.0.0.1:8601")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="Sim12345")
    a = ap.parse_args()
    Gst.init(None)
    server = GstRtspServer.RTSPServer()
    server.set_service(str(a.port))
    # Hikvision kabi Digest autentifikatsiya
    auth = GstRtspServer.RTSPAuth()
    token = GstRtspServer.RTSPToken()
    token.set_string("media.factory.role", "viewer")
    auth.add_digest(a.user, a.password, token)
    auth.set_supported_methods(GstRtsp.RTSPAuthMethod.DIGEST)
    server.set_auth(auth)
    mounts = server.get_mount_points()
    for i in range(1, a.cameras + 1):
        f = GstRtspServer.RTSPMediaFactory()
        f.set_launch(
            f"( souphttpsrc location={a.camsim}/cam{i}.mjpg is-live=true do-timestamp=true ! multipartdemux ! "
            "jpegdec ! videoconvert ! videorate ! video/x-raw,framerate=10/1 ! "
            "x264enc tune=zerolatency speed-preset=ultrafast key-int-max=10 bitrate=800 ! "
            "rtph264pay name=pay0 pt=96 config-interval=1 )"
        )
        f.set_shared(True)
        f.add_role_from_structure(Gst.Structure.new_from_string(
            "viewer, media.factory.access=(boolean)true, media.factory.construct=(boolean)true"))
        mounts.add_factory(f"/Streaming/Channels/{i}02", f)
    server.attach(None)
    print(f"RTSP: rtsp://{a.user}:****@127.0.0.1:{a.port}/Streaming/Channels/102 …", flush=True)
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
