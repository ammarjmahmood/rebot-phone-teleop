import os
import sys
import time
import urllib.request

URL = os.environ.get("REBOT_KIOSK_URL", "http://127.0.0.1:8081/")


def wait_for_server(timeout=180):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            urllib.request.urlopen(URL + "health", timeout=2)
            return True
        except OSError:
            time.sleep(1.0)
    return False


def main():
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("WebKit2", "4.0")
    from gi.repository import Gdk, Gtk, WebKit2
    wait_for_server()
    window = Gtk.Window(title="reBot")
    window.set_default_size(1280, 800)
    view = WebKit2.WebView()
    settings = view.get_settings()
    settings.set_enable_webgl(True)
    settings.set_hardware_acceleration_policy(WebKit2.HardwareAccelerationPolicy.ALWAYS)
    settings.set_enable_developer_extras(True)
    view.load_uri(URL)

    def on_key(widget, event):
        if event.keyval == Gdk.KEY_F11:
            if window.get_window().get_state() & Gdk.WindowState.FULLSCREEN:
                window.unfullscreen()
            else:
                window.fullscreen()
        elif event.keyval == Gdk.KEY_F5:
            view.reload()
        return False

    def on_failed(webview, event, uri, error):
        Gtk.timeout_add_seconds(3, lambda: view.load_uri(URL) and False)
        return False

    view.connect("load-failed", on_failed)
    window.connect("key-press-event", on_key)
    window.connect("destroy", Gtk.main_quit)
    window.add(view)
    window.show_all()
    if "--windowed" not in sys.argv:
        window.fullscreen()
    Gtk.main()


if __name__ == "__main__":
    main()
