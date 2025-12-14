import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib
import bluetooth
import threading
import socket
import sys
import os
import signal
from enum import Enum

class Status(Enum):
    DISCONNECTED = 0
    CONNECTING = 1
    CONNECTED = 2

class Mode(Enum):
    NONE = 0
    SERVER = 1
    CLIENT = 2

AGENT_PATH = "/test/agent"

rfcomm_sock = 0
server_sock = 0
client_sock = 0

mode = Mode.NONE
status = Status.DISCONNECTED

t_server = 0
t_client = 0
t_serial = 0

client_sem = threading.Semaphore(1)

my_addr = bluetooth.read_local_bdaddr()[0]
remote_addr = "None"
pipe_file = 0

class Rejected(dbus.DBusException):
    _dbus_error_name = "org.bluez.Error.Rejected"

class Agent(dbus.service.Object):
    def __init__(self, bus, path):
        super().__init__(bus, path)

    # Request a PIN code (legacy devices)
    @dbus.service.method("org.bluez.Agent1",
                         in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        print("RequestPinCode for", device)
        return "1234"                 # fixed PIN if needed

    # RequestPasskey (used by some devices)
    @dbus.service.method("org.bluez.Agent1",
                         in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        print("RequestPasskey for", device)
        return dbus.UInt32(1234)

    # DisplayPasskey: device, passkey (u), entered (q)
    @dbus.service.method("org.bluez.Agent1",
                         in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        print(f"DisplayPasskey {device} passkey={passkey} entered={entered}")

    # DisplayPinCode: device, pincode
    @dbus.service.method("org.bluez.Agent1",
                         in_signature="os", out_signature="")
    def DisplayPinCode(self, device, pincode):
        print(f"DisplayPinCode {device} pincode={pincode}")

    # RequestConfirmation: device, passkey
    @dbus.service.method("org.bluez.Agent1",
                         in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        print(f"RequestConfirmation {device} passkey={passkey} -> auto-accept")
        # auto-accept (do nothing / return)
        return

    # AuthorizeService: device, uuid
    @dbus.service.method("org.bluez.Agent1",
                         in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        print(f"AuthorizeService {device} uuid={uuid} -> auto-allow")
        # auto-allow

    @dbus.service.method("org.bluez.Agent1",
                         in_signature="", out_signature="")
    def Cancel(self):
        print("Agent Cancelled")

def register_agent():
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    system_bus = dbus.SystemBus()
    agent = Agent(system_bus, AGENT_PATH)

    manager = dbus.Interface(system_bus.get_object("org.bluez", "/org/bluez"),
                             "org.bluez.AgentManager1")
    try:
        manager.RegisterAgent(AGENT_PATH, "NoInputNoOutput")
    except dbus.exceptions.DBusException as e:
        # if already registered, ignore
        print("RegisterAgent:", e.get_dbus_message() if hasattr(e, "get_dbus_message") else e)
    try:
        manager.RequestDefaultAgent(AGENT_PATH)
    except dbus.exceptions.DBusException as e:
        print("RequestDefaultAgent:", e)
    print("Agent registered as NoInputNoOutput")
    return agent

def rfcomm_server():
    global mode
    global status
    global server_sock
    global rfcomm_sock

    print("Server thread entry")

    while True:
        try:
            server_sock = bluetooth.BluetoothSocket(bluetooth.RFCOMM)
            server_sock.bind(("", 1))
            server_sock.listen(1)

            print("Listening on RFCOMM port 1")
            rfcomm_sock, addr = server_sock.accept()

            if (mode == Mode.CLIENT):
                rfcomm_sock.close()
                server_sock.close()
                print("Accepted master mode connection while in client mode, disconnecting")
                continue

            mode = Mode.SERVER
            status = Status.CONNECTED

            print("Connected to remote", addr[0], "as master")
            while True:
                data = rfcomm_sock.recv(1024)
                if not data:
                    break

                rfcomm_msg_received(data.decode().strip())

        except Exception as e:
            print("RFCOMM server error:", e)
        finally:
            try:
                server_sock.close()
                rfcomm_sock.close()
            except:
                pass
        
        mode = Mode.NONE
        status = Status.DISCONNECTED
        print("Disconnected from ", addr)

    print("Server thread exit")

def rfcomm_client():
    global mode
    global status
    global client_sem
    global client_sock

    print("Client thread entry")

    while True:

        try:
            client_sem.acquire()
            client_sem.acquire()
            
            print("Connecting to remote " + str(remote_addr))

            if (mode == Mode.SERVER):
                print("Attempted client connection while in server mode, cancelling")
                
                client_sem.release()
                continue

            mode = Mode.CLIENT
            status = Status.CONNECTING

            client_sock = bluetooth.BluetoothSocket(bluetooth.RFCOMM)
            # client_sock.settimeout(5.0)
            client_sock.connect((remote_addr, 1))

            status = Status.CONNECTED
            print("Connected to remote " + str(remote_addr) + " as client")

            while True:
                try:
                    data = client_sock.recv(1024)  # max 1024 bytes
                    if not data:
                        break
                    
                    rfcomm_msg_received(data.decode().strip())

                except bluetooth.BluetoothError:
                    print("No data received (timeout or disconnect)")
                    break
                
            print("Disconnected from remote " + str(remote_addr))
            status = Status.DISCONNECTED
            mode = Mode.NONE

        except Exception as e:
            print("RFCOMM client error:", e)
            client_sock.close()
            print("Disconnected from remote " + str(remote_addr))
            status = Status.DISCONNECTED
            mode = Mode.NONE

        finally:
            client_sem.release()
    
    print("Client thread exit")

def rfcomm_server_start():
    global t_server

    t_server = threading.Thread(target=rfcomm_server, daemon=True)
    t_server.start()

def rfcomm_client_start():
    global t_client

    t_client = threading.Thread(target=rfcomm_client, daemon=True)
    t_client.start()

def rfcomm_client_connect(addr):
    global remote_addr
    global t_client

    if (t_client.is_alive() != True):
        print("Can't connect while client is disabled")
        return

    remote_addr = addr.strip()

    client_sem.release()

def rfcomm_disconnect():
    global client_sock
    global rfcomm_sock
    global server_sock
    
    if rfcomm_sock:
        try:
            rfcomm_sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        finally:
            try:
                rfcomm_sock.close()
            except:
                pass
    
    if server_sock:
        try:
            server_sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        finally:
            try:
                server_sock.close()
            except:
                pass
    
    if client_sock:
        try:
            client_sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        finally:
            try:
                client_sock.close()
            except:
                pass

def rfcomm_send_msg(msg):
    global mode
    global status

    if (status != Status.CONNECTED):
        print("Send msg failed: bad connection status")
        return
    
    if (mode == Mode.SERVER):
        rfcomm_sock.send(msg)
    elif (mode == Mode.CLIENT):
        client_sock.send(msg)
    else:
        print("Send msg failed: bad connection mode")
        return

def rfcomm_msg_received(msg):
    print("Message received:", str(msg))

def print_status():
    if (mode == Mode.NONE):
        print("Mode: Unassigned")
    elif (mode == Mode.SERVER):
        print("Mode: Server")
    elif (mode == Mode.CLIENT):
        print("Mode: Client")

    if (status == Status.CONNECTED):
        print("Status:         Connected")
        print("Local Address:  " + my_addr)
        print("Remote Address: " + remote_addr)
    elif (status == Status.DISCONNECTED):
        print("Status:         Disconnected")
        print("Local Address:  " + my_addr)

def serial_console():
    global server_sock
    global rfcomm_sock

    print("Starting serial console")

    while True:
        text = str(input())
        text_split = text.split()

        if (len(text_split) < 1):
            continue

        if text_split[0] == "echo":
            print(text)
        elif text_split[0] == "send":
            print("sending" + text[len(text_split[0]):])
            rfcomm_send_msg(text[len(text_split[0]):])
        elif text_split[0] == "status":
            print_status()
        elif text_split[0] == "addr":
            remote_addr = text_split[1]
            print("Remote address: " + text_split[1])
        elif text_split[0] == "connect":
            rfcomm_client_connect(remote_addr)
        elif text_split[0] == "disconnect":
            rfcomm_disconnect()
            
def addr_pipe_handler():
    global remote_addr
    
    pipe_file = os.getenv("BT_ADDR_PIPE")

    pipe = open(pipe_file)

    for line in pipe:
        print("Received address from address pipe:", line)
        remote_addr = line
        rfcomm_client_connect(remote_addr)

def send_bt_msg(msg_str):
    print("Sending bt message: [ " + msg_str + " ]")

def receive_bt_msg(msg_str):
    print("Received bt message: [ " + msg_str + " ]")

def cleanup(signum=None, frame=None):
    global pipe_file

    rfcomm_disconnect()

    try:
        if (pipe_file):
            pipe_file.close()
    finally:
        print("Pipe closed")

    sys.exit(0)

def main():
    global t_server, t_serial
    # Register agent on system bus
    register_agent()
    
    t_serial = threading.Thread(target=serial_console, daemon=True)
    t_serial.start()

    t_pipe = threading.Thread(target=addr_pipe_handler, daemon=True)
    t_pipe.start()

    # Start the server
    rfcomm_server_start()

    # Start the client thread
    rfcomm_client_start()

    # Run GLib main loop to serve DBus methods (agent)
    try:
        GLib.MainLoop().run()
    except KeyboardInterrupt:
        print("Exiting")

signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)

try:
    main()
finally:
    cleanup()