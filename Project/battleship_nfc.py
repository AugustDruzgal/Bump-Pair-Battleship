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
import pygame
import pigame
from pygame.locals import *
import json
import time
import queue
import RPi.GPIO as GPIO
from enum import Enum
import random
import math

os.environ["SDL_VIDEODRIVER"] = "fbcon"
os.environ["SDL_FBDEV"] = "/dev/fb0"
os.environ["SDL_MOUSEDRV"] = "dummy"
os.environ["SDL_MOUSEDEV"] = "/dev/null"
os.environ["DISPLAY"] = ""

GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)
BUTTON_RIGHT  = 27
BUTTON_DOWN   = 23
BUTTON_ROTATE = 22
BUTTON_SELECT = 17

buttons = [BUTTON_SELECT, BUTTON_ROTATE, BUTTON_DOWN, BUTTON_RIGHT]
for pin in buttons:
    GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

quit_press_start = None
QUIT_HOLD_TIME = 3.0

tx_queue = queue.Queue()
rx_queue = queue.Queue()

class Status(Enum):
    DISCONNECTED = 0
    CONNECTING = 1
    CONNECTED = 2

class Mode(Enum):
    NONE = 0
    SERVER = 1
    CLIENT = 2

AGENT_PATH = "/test/agent"
rfcomm_sock = None
server_sock = None
client_sock = None
mode = Mode.NONE
status = Status.DISCONNECTED
client_sem = threading.Semaphore(0)
my_addr = bluetooth.read_local_bdaddr()[0]
target_addr = None
IS_MASTER_PI = False
reset_needed = False
running = True

connection_enabled = threading.Event()
handshake_sent = False
handshake_complete = False

class Agent(dbus.service.Object):
    def __init__(self, bus, path):
        super().__init__(bus, path)

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="s")
    def RequestPinCode(self, device): return "1234"

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="u")
    def RequestPasskey(self, device): return dbus.UInt32(1234)

    @dbus.service.method("org.bluez.Agent1", in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered): pass

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def DisplayPinCode(self, device, pincode): pass

    @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey): return

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid): return

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Cancel(self): pass

def register_agent():
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    system_bus = dbus.SystemBus()
    agent = Agent(system_bus, AGENT_PATH)
    manager = dbus.Interface(system_bus.get_object("org.bluez", "/org/bluez"), "org.bluez.AgentManager1")
    try:
        manager.RegisterAgent(AGENT_PATH, "NoInputNoOutput")
        manager.RequestDefaultAgent(AGENT_PATH)
    except Exception: pass
    return agent

def nfc_pipe_watcher():
    global target_addr
    pipe_path = os.environ.get("BT_ADDR_PIPE", "/home/pi/Project/bt_addr_pipe")
    print(f"NFC: Watching pipe {pipe_path}...")
    
    last_nfc_time = 0
    NFC_COOLDOWN = 3.0
    
    while not os.path.exists(pipe_path):
        time.sleep(1)
        
    try:
        pipe = open(pipe_path, "r")
        for line in pipe:
            clean_addr = line.strip()
            if len(clean_addr) == 17 and clean_addr.count(':') == 5:
                if time.time() - last_nfc_time < NFC_COOLDOWN:
                    continue
                
                if status != Status.DISCONNECTED:
                    continue

                print(f"NFC: Received Address from Pipe: {clean_addr}")
                last_nfc_time = time.time()
                target_addr = clean_addr
                client_sem.release()
    except Exception as e:
        print(f"NFC Pipe Error: {e}")

def rfcomm_server():
    global mode, status, server_sock, rfcomm_sock, IS_MASTER_PI, reset_needed
    print("NET: Server thread started (Initializing Socket...)")
    
    try:
        server_sock = bluetooth.BluetoothSocket(bluetooth.RFCOMM)
        server_sock.bind(("", 1)) 
        server_sock.listen(1)
        print("NET: Server Socket Initialized on Port 1. Waiting for Gate to Open.")
    except Exception as e:
        print(f"NET CRITICAL: Could not bind server socket: {e}")
        return

    while True:
        try:
            if not connection_enabled.is_set():
                connection_enabled.wait()
                print("NET: Server Gate Opened - Accepting Connections Now")

            new_client, addr = server_sock.accept()
            
            if not connection_enabled.is_set():
                new_client.close()
                continue

            if mode == Mode.CLIENT:
                new_client.close()
                continue

            print(f"NET: Accepted connection from {addr}. I am SERVER.")
            rfcomm_sock = new_client 
            mode = Mode.SERVER
            status = Status.CONNECTED
            IS_MASTER_PI = True 
            
            buffer = ""
            while True:
                try:
                    data = rfcomm_sock.recv(1024)
                    if not data: 
                        print("NET: Remote closed connection.")
                        break
                    buffer += data.decode('utf-8')
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        if line: rx_queue.put(line.strip())
                except Exception:
                    break 
                    
        except Exception as e:
            break
        finally:
            if status == Status.CONNECTED:
                print("NET: Connection lost. Triggering Reset.")
                reset_needed = True
            
            status = Status.DISCONNECTED
            mode = Mode.NONE
            try: rfcomm_sock.close()
            except: pass
            time.sleep(0.1)

def rfcomm_client():
    global mode, status, client_sock, target_addr, client_sem, IS_MASTER_PI, reset_needed
    
    print("NET: Client thread started (Waiting for NFC...)")
    
    while True:
        try:
            client_sem.acquire()
            
            if not connection_enabled.is_set():
                print("NET: NFC ignored - Game is on Start Screen.")
                continue

            if status != Status.DISCONNECTED:
                print("NET: NFC ignored in Client Thread - Already Connected/Connecting.")
                continue

            print(f"NET: NFC Triggered! Attempting to connect to {target_addr}")
            if mode == Mode.SERVER:
                print("NET: Already connected as Server. Cancelling Client attempt.")
                continue

            mode = Mode.CLIENT
            status = Status.CONNECTING
            
            client_sock = bluetooth.BluetoothSocket(bluetooth.RFCOMM)
            client_sock.connect((target_addr, 1))
            
            print(f"NET: Connected to {target_addr}. I am CLIENT.")
            status = Status.CONNECTED
            IS_MASTER_PI = False
            
            buffer = ""
            while True:
                try:
                    data = client_sock.recv(1024)
                    if not data: 
                        print("NET: Remote closed connection.")
                        break
                    buffer += data.decode('utf-8')
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        if line: rx_queue.put(line.strip())
                except Exception:
                    break
        except Exception as e:
            print(f"CLIENT ERROR: {e}")
            if client_sock:
                try: client_sock.close()
                except: pass
            status = Status.DISCONNECTED
            mode = Mode.NONE
        finally:
            if status == Status.CONNECTED:
                print("NET: Connection lost. Triggering Reset.")
                reset_needed = True
            pass

def rfcomm_send_msg(msg):
    try:
        data_str = str(msg) + "\n"
        data_bytes = data_str.encode('utf-8')
        if mode == Mode.SERVER and rfcomm_sock:
            rfcomm_sock.send(data_bytes)
        elif mode == Mode.CLIENT and client_sock:
            client_sock.send(data_bytes)
    except Exception as e:
        print(f"Send Error: {e}")

def tx_queue_worker():
    while True:
        msg = tx_queue.get()
        if status == Status.CONNECTED:
            rfcomm_send_msg(msg)
        tx_queue.task_done()

def reset_game_state():
    global game_state, is_connected, done_placing_ships, opponent_ready
    global first_turn_started, first_turn_decided, has_first_turn
    global shooting_result_received, shooting_result_sent, game_over, shot_fired
    global ship_positions, shots_fired, my_board_shots, occupied_placement
    global processed_shot_coords, ship_placement_index, current_ship_length
    global mode, status, server_sock, rfcomm_sock, client_sock, reset_needed
    global DISPLAY_MESSAGE, target_addr
    global handshake_sent, handshake_complete
    global shooting_cursor_pos, current_ship_orientation
    global particles, shake_end_time, flash_alpha, floating_texts

    print("GAME: Performing Soft Reset to START SCREEN...")
    
    connection_enabled.clear()

    try:
        if rfcomm_sock: rfcomm_sock.shutdown(socket.SHUT_RDWR); rfcomm_sock.close()
    except: pass
    try:
        if client_sock: client_sock.shutdown(socket.SHUT_RDWR); client_sock.close()
    except: pass

    handshake_sent = False
    handshake_complete = False

    game_state = "START_SCREEN"
    is_connected = False
    done_placing_ships = False
    opponent_ready = False
    first_turn_started = False
    first_turn_decided = False
    has_first_turn = False
    shooting_result_received = False
    shooting_result_sent = False
    game_over = False
    shot_fired = False
    
    ship_positions = {}
    shots_fired = {}
    my_board_shots = {}
    occupied_placement = set()
    processed_shot_coords = set()
    
    shooting_cursor_pos = (0, 0)
    current_ship_orientation = "horizontal"
    
    ship_placement_index = 0
    current_ship_length = SHIPS_TO_PLACE[0]
    target_addr = None
    
    mode = Mode.NONE
    status = Status.DISCONNECTED
    reset_needed = False
    
    particles = []
    floating_texts = []
    shake_end_time = 0
    flash_alpha = 0
    
    with tx_queue.mutex: tx_queue.queue.clear()
    with rx_queue.mutex: rx_queue.queue.clear()
    
    DISPLAY_MESSAGE = ""

def check_quit_button():
    global quit_press_start, running
    if not GPIO.input(BUTTON_SELECT):
        if quit_press_start is None:
            quit_press_start = time.time()
        else:
            if time.time() - quit_press_start >= QUIT_HOLD_TIME:
                if game_state == "START_SCREEN":
                    print("USER: Exiting Program from Start Screen.")
                    running = False 
                else:
                    print("USER: Resetting to Start Screen...")
                    send_data({"type": "DISCONNECT"})
                    time.sleep(0.5) 
                    reset_game_state()
                    quit_press_start = None 
    else:
        quit_press_start = None

GRID_SIZE = 5
CELL_SIZE = 40 
GRID_OFFSET_X = 20 
GRID_OFFSET_Y = 60 

LINE_COLOR = (0, 0, 0)
WATER_COLOR = (240, 240, 240)
CURSOR_COLOR = (0, 100, 0)
RETICLE_COLOR = (255, 0, 0)
MISS_COLOR = (255, 0, 0)
HIT_COLOR = (200, 0, 0)
INVALID_COLOR = (255, 100, 0)  
TEXT_COLOR = (0, 0, 0)            
ICON_COLOR = (0, 0, 0)

PARTICLE_COLORS = [(255, 100, 0), (255, 50, 0), (255, 200, 0)]
WATER_PARTICLE_COLORS = [(150, 200, 255), (100, 150, 255), (200, 200, 255)]
SHAKE_DURATION = 0.25
SHAKE_INTENSITY = 3
FLASH_INTENSITY = 180

game_state = "START_SCREEN"
connection_enabled.clear()

logo_base_y = 110  
logo_y = logo_base_y
blink_timer = 0
show_blink = True

is_connected = False
done_placing_ships = False
opponent_ready = False 
first_turn_started = False
first_turn_decided = False
has_first_turn = False
shooting_result_received = False
shooting_result_sent = False
game_over = False
shot_fired = False

DISPLAY_MESSAGE = "" 
MESSAGE_DISPLAY_TIME = 0 
result_display_time = 0 

ship_positions = {}
shots_fired = {}
shooting_cursor_pos = (0, 0) 
my_board_shots = {}
SHIPS_TO_PLACE = [3, 2] 
ship_placement_index = 0
current_ship_length = SHIPS_TO_PLACE[0]
current_ship_orientation = "horizontal"
occupied_placement = set()
processed_shot_coords = set()
last_sent_shot = None
message_sequence = 0
waiting_for_opponent_ready = False

particles = []
floating_texts = []
shake_end_time = 0
flash_alpha = 0

pygame.init()
pitft = pigame.PiTft() 
screen = pygame.display.set_mode((320, 240))
pygame.mouse.set_visible(False) 
pygame.display.set_caption("Battleship")

FONT = pygame.font.Font(None, 25)
SMALL_FONT = pygame.font.Font(None, 20)
BIG_FONT = pygame.font.Font(None, 40) 
HUGE_FONT = pygame.font.Font(None, 60)
CLOCK = pygame.time.Clock()

background_img = None 

try:
    ship_img_2 = pygame.image.load("ShipDestroyerHull.png").convert_alpha()
    ship_img_3 = pygame.image.load("ShipCruiserHull.png").convert_alpha()
    ship_assets = {
        2: pygame.transform.scale(ship_img_2, (CELL_SIZE, CELL_SIZE * 2)),
        3: pygame.transform.scale(ship_img_3, (CELL_SIZE, CELL_SIZE * 3))
    }
    
    hit_raw = pygame.image.load("hit.png").convert_alpha()
    marker_assets = {
        "HIT": pygame.transform.scale(hit_raw, (CELL_SIZE, CELL_SIZE))
    }
    
    bg_raw = pygame.image.load("background.png").convert()
    background_img = pygame.transform.scale(bg_raw, (240, 360))
    
except Exception as e:
    print(f"ERROR LOADING IMAGES: {e}")
    ship_assets = {}
    marker_assets = {} 
    background_img = None 

def send_data(data):
    global message_sequence
    try:
        data['seq'] = message_sequence; message_sequence += 1
        tx_queue.put(json.dumps(data))
        return True
    except Exception: return False

def receive_data():
    try:
        data = json.loads(rx_queue.get(block=False))
        if data and data.get("type") == "DISCONNECT":
            print("GAME: Received Disconnect Signal from Opponent.")
            reset_game_state()
            return None
        return data
    except queue.Empty:
        return None
    except Exception:
        return None

def trigger_explosion(grid_coord, label_text=None):
    global shake_end_time, flash_alpha
    
    cx = GRID_OFFSET_X + grid_coord[0] * CELL_SIZE + CELL_SIZE // 2
    cy = GRID_OFFSET_Y + grid_coord[1] * CELL_SIZE + CELL_SIZE // 2
    
    is_miss = (label_text == "MISS!")
    
    shake_end_time = time.time() + SHAKE_DURATION
    
    if not is_miss:
        flash_alpha = FLASH_INTENSITY

    if not is_miss:
        for _ in range(30):
            particles.append({
                "x": cx,
                "y": cy,
                "vx": random.uniform(-2, 2),  
                "vy": random.uniform(-2, 2), 
                "size": random.randint(3, 6),
                "color": random.choice(PARTICLE_COLORS),
                "life": 1.0 
            })

    if label_text:
        text_col = (255, 50, 50) 
        
        txt_surf = FONT.render(label_text, True, text_col)
        floating_texts.append({
            "x": cx - txt_surf.get_width() // 2,
            "y": cy - 20,
            "surf": txt_surf,
            "life": 255.0
        })

def update_and_draw_vfx(surface):
    global particles, flash_alpha, floating_texts
    
    for i in range(len(particles) - 1, -1, -1):
        p = particles[i]
        p["x"] += p["vx"]
        p["y"] += p["vy"]
        p["life"] -= 0.04 
        
        if p["life"] <= 0:
            particles.pop(i)
        else:
            current_size = int(p["size"] * p["life"])
            if current_size > 0:
                rect = (int(p["x"] - current_size/2), int(p["y"] - current_size/2), current_size, current_size)
                pygame.draw.rect(surface, p["color"], rect)

    for i in range(len(floating_texts) - 1, -1, -1):
        ft = floating_texts[i]
        ft["y"] -= 0.5 
        
        ft["life"] -= 1.5 
        
        if ft["life"] <= 0:
            floating_texts.pop(i)
        else:
            ft["surf"].set_alpha(int(ft["life"]))
            surface.blit(ft["surf"], (ft["x"], ft["y"]))

    if flash_alpha > 0:
        flash_surf = pygame.Surface(surface.get_size())
        flash_surf.fill((255, 255, 255))
        flash_surf.set_alpha(flash_alpha)
        surface.blit(flash_surf, (0, 0))
        flash_alpha -= 15

def get_ship_positions(start, length, orientation):
    x, y = start
    if orientation == "horizontal": return [(x + i, y) for i in range(length)]
    else: return [(x, y + i) for i in range(length)]

def in_bounds(positions):
    return all(0 <= x < GRID_SIZE and 0 <= y < GRID_SIZE for x, y in positions)

def ship_overlaps(positions, occupied):
    return any(pos in occupied for pos in positions)

def check_if_sunk(ship_parts):
    return all(part["hit"] for part in ship_parts)

def check_for_game_over():
    return all(ship["sunk"] for ship in ship_positions.values())

def get_ship_part_at(coord):
    for ship in ship_positions.values():
        for part in ship['parts']:
            if part['pos'] == coord: return part
    return None

def draw_text(surface, text, pos, color=LINE_COLOR, font=SMALL_FONT):
    text_surface = font.render(text, True, color)
    surface.blit(text_surface, pos)

def draw_icon(surface, shape, center_pos, size=15, color=ICON_COLOR):
    x, y = center_pos
    if shape == "RIGHT_ARROW":
        points = [(x - size//2, y - size//2), (x - size//2, y + size//2), (x + size//2, y)]
        pygame.draw.polygon(surface, color, points)
    elif shape == "DOWN_ARROW":
        points = [(x - size//2, y - size//2), (x + size//2, y - size//2), (x, y + size//2)]
        pygame.draw.polygon(surface, color, points)

def draw_reticle(surface, cursor_pos):
    cx = GRID_OFFSET_X + cursor_pos[0] * CELL_SIZE + CELL_SIZE // 2
    cy = GRID_OFFSET_Y + cursor_pos[1] * CELL_SIZE + CELL_SIZE // 2
    radius = CELL_SIZE // 2 - 2
    
    pygame.draw.circle(surface, RETICLE_COLOR, (cx, cy), radius, 2)
    pygame.draw.line(surface, RETICLE_COLOR, (cx - radius, cy), (cx + radius, cy), 2)
    pygame.draw.line(surface, RETICLE_COLOR, (cx, cy - radius), (cx, cy + radius), 2)

def draw_miss_x(surface, coord):
    x = GRID_OFFSET_X + coord[0] * CELL_SIZE
    y = GRID_OFFSET_Y + coord[1] * CELL_SIZE
    
    margin = 5
    pygame.draw.line(surface, MISS_COLOR, (x + margin, y + margin), (x + CELL_SIZE - margin, y + CELL_SIZE - margin), 4)
    pygame.draw.line(surface, MISS_COLOR, (x + CELL_SIZE - margin, y + margin), (x + margin, y + CELL_SIZE - margin), 4)

def draw_grid(is_shooting_board, cursor_pos=None, temp_ship_positions=None):
    canvas = pygame.Surface((240, 320)) 
    
    canvas.fill(WATER_COLOR)
    
    if background_img:
        wave_offset = -20 + math.sin(time.time() * 1.5) * 4
        canvas.blit(background_img, (0, int(wave_offset)))
    
    for x in range(GRID_SIZE):
        for y in range(GRID_SIZE):
            rect = pygame.Rect(GRID_OFFSET_X + x * CELL_SIZE, GRID_OFFSET_Y + y * CELL_SIZE, CELL_SIZE, CELL_SIZE)
            pygame.draw.rect(canvas, LINE_COLOR, rect, 1) 
            
    if not is_shooting_board:
        for ship_name, ship_data in ship_positions.items():
            parts = ship_data["parts"]
            if not parts: continue
            sorted_parts = sorted(parts, key=lambda p: (p['pos'][1], p['pos'][0]))
            head_x, head_y = sorted_parts[0]['pos']
            length = len(parts)
            is_horizontal = False
            if length > 1:
                if sorted_parts[0]['pos'][1] == sorted_parts[1]['pos'][1]: 
                    is_horizontal = True
            
            if length in ship_assets:
                img = ship_assets[length]
                if is_horizontal: img = pygame.transform.rotate(img, 90)
                px = GRID_OFFSET_X + head_x * CELL_SIZE
                py = GRID_OFFSET_Y + head_y * CELL_SIZE
                canvas.blit(img, (px, py))
            else:
                for part in parts:
                    cx, cy = part['pos']
                    r = pygame.Rect(GRID_OFFSET_X + cx*CELL_SIZE, GRID_OFFSET_Y + cy*CELL_SIZE, CELL_SIZE, CELL_SIZE)
                    pygame.draw.rect(canvas, (100,100,100), r)

        if temp_ship_positions and game_state == "PLACING_SHIPS":
            is_valid = in_bounds(temp_ship_positions) and not ship_overlaps(temp_ship_positions, occupied_placement)
            if current_ship_length in ship_assets:
                preview_img = ship_assets[current_ship_length].copy()
                if current_ship_orientation == "horizontal":
                    preview_img = pygame.transform.rotate(preview_img, 90)
                
                if not is_valid:
                    tint = pygame.Surface(preview_img.get_size(), pygame.SRCALPHA)
                    tint.fill((255, 0, 0, 100)) 
                    preview_img.blit(tint, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
                else:
                    preview_img.set_alpha(150)
                
                px = GRID_OFFSET_X + shooting_cursor_pos[0] * CELL_SIZE
                py = GRID_OFFSET_Y + shooting_cursor_pos[1] * CELL_SIZE
                canvas.blit(preview_img, (px, py))
            else:
                fill_color = (150, 150, 150) if is_valid else INVALID_COLOR
                for (tx, ty) in temp_ship_positions:
                    r = pygame.Rect(GRID_OFFSET_X + tx*CELL_SIZE, GRID_OFFSET_Y + ty*CELL_SIZE, CELL_SIZE, CELL_SIZE)
                    pygame.draw.rect(canvas, fill_color, r)
                    pygame.draw.rect(canvas, LINE_COLOR, r, 1)

    for x in range(GRID_SIZE):
        for y in range(GRID_SIZE):
            coord = (x, y)
            if is_shooting_board and coord in shots_fired:
                result = shots_fired[coord]
                if result == "MISS": 
                    draw_miss_x(canvas, coord)
                elif result in ["HIT", "SUNK", "ALL_SUNK"]: 
                    draw_marker(canvas, coord, HIT_COLOR)
            elif not is_shooting_board:
                ship_part = get_ship_part_at(coord)
                if ship_part and ship_part['hit']:
                    draw_marker(canvas, coord, HIT_COLOR)
                elif coord in my_board_shots and my_board_shots[coord] == "MISS":
                    draw_miss_x(canvas, coord)

    if cursor_pos:
        if is_shooting_board:
            draw_reticle(canvas, cursor_pos)
        else:
            rect = pygame.Rect(GRID_OFFSET_X + cursor_pos[0] * CELL_SIZE, GRID_OFFSET_Y + cursor_pos[1] * CELL_SIZE, CELL_SIZE, CELL_SIZE)
            pygame.draw.rect(canvas, CURSOR_COLOR, rect, 3)
    
    return canvas

def draw_marker(surface, coord, color):
    x = GRID_OFFSET_X + coord[0] * CELL_SIZE
    y = GRID_OFFSET_Y + coord[1] * CELL_SIZE

    if color == HIT_COLOR and "HIT" in marker_assets:
         surface.blit(marker_assets["HIT"], (x, y))
    else:
         center_x = x + CELL_SIZE // 2
         center_y = y + CELL_SIZE // 2
         radius = CELL_SIZE // 4
         pygame.draw.circle(surface, color, (center_x, center_y), radius)

def update_start_screen_anim():
    global logo_y, blink_timer, show_blink
    logo_y = logo_base_y + math.sin(time.time() * 5) * 5
    if time.time() - blink_timer > 0.8:
        show_blink = not show_blink
        blink_timer = time.time()

def draw_start_screen():
    canvas = pygame.Surface((240, 320))
    canvas.fill((0, 0, 50)) 
    logo_surf = BIG_FONT.render("BATTLESHIP", True, (255, 255, 255))
    logo_rect = logo_surf.get_rect(center=(120, int(logo_y)))
    canvas.blit(logo_surf, logo_rect)
    if show_blink:
        text_surf = FONT.render("Tap screen to start", True, (0, 255, 0))
        text_rect = text_surf.get_rect(center=(120, 240)) 
        canvas.blit(text_surf, text_rect)
    pygame.draw.rect(canvas, (200, 0, 0), (160, 280, 70, 30))
    draw_text(canvas, "QUIT", (175, 287), (255, 255, 255), SMALL_FONT)
    return canvas

def update_screen():
    global screen, DISPLAY_MESSAGE, MESSAGE_DISPLAY_TIME
    
    if game_state == "START_SCREEN":
        update_start_screen_anim()
        canvas = draw_start_screen()
    
    elif game_state == "PLACING_SHIPS":
        temp_ship_positions = None
        if not done_placing_ships:
            temp_ship_positions = get_ship_positions(shooting_cursor_pos, current_ship_length, current_ship_orientation)
        canvas = draw_grid(False, shooting_cursor_pos, temp_ship_positions)
        status_text = f"Setup: {current_ship_length} ({current_ship_orientation[0].upper()})"
        draw_text(canvas, status_text, (60, 20), LINE_COLOR)
        draw_icon(canvas, "RIGHT_ARROW", (40, 290)) 
        draw_icon(canvas, "DOWN_ARROW", (90, 290)) 
        draw_text(canvas, "Rot", (125, 283), ICON_COLOR) 
        draw_text(canvas, "Sel", (190, 283), ICON_COLOR) 

    elif game_state == "SHOOTING":
        canvas = draw_grid(True, shooting_cursor_pos if not shot_fired else None)
        draw_text(canvas, "SHOOTING", (80, 20), LINE_COLOR)
        draw_icon(canvas, "RIGHT_ARROW", (40, 290)) 
        draw_icon(canvas, "DOWN_ARROW", (90, 290)) 
        draw_text(canvas, "Sel", (190, 283), ICON_COLOR) 

    elif game_state == "RECEIVING":
        canvas = draw_grid(False)
        draw_text(canvas, "RECEIVING", (80, 20), LINE_COLOR)

    else:
        canvas = pygame.Surface((240, 320))
        canvas.fill(WATER_COLOR)
        if game_state == "WAITING": 
            pulse_scale = 1.0 + 0.05 * math.sin(time.time() * 5)

            if status == Status.CONNECTED and not handshake_complete:
                 text_surf = BIG_FONT.render("Syncing...", True, LINE_COLOR)
                 canvas.blit(text_surf, text_surf.get_rect(center=(120, 140)))
                 
                 sub_surf = FONT.render("Wait for them...", True, (100,100,100))
                 canvas.blit(sub_surf, sub_surf.get_rect(center=(120, 180)))
            
            elif status == Status.CONNECTING:
                 text_surf = BIG_FONT.render("Connecting...", True, LINE_COLOR)
                 canvas.blit(text_surf, text_surf.get_rect(center=(120, 140)))
                 
                 sub_surf = FONT.render("Please wait...", True, (100,100,100))
                 canvas.blit(sub_surf, sub_surf.get_rect(center=(120, 180)))
            
            else:
                 text_surf = BIG_FONT.render("Searching...", True, LINE_COLOR)
                 canvas.blit(text_surf, text_surf.get_rect(center=(120, 140)))
                 
                 base_sub = FONT.render("Tap NFC to Start", True, (100,100,100))
                 
                 w = int(base_sub.get_width() * pulse_scale)
                 h = int(base_sub.get_height() * pulse_scale)
                 pulsed_sub = pygame.transform.scale(base_sub, (w, h))
                 
                 canvas.blit(pulsed_sub, pulsed_sub.get_rect(center=(120, 180)))
        
        elif game_state == "DECIDING_FIRST_TURN": 
             pass
             
        elif game_state == "END": 
            update_start_screen_anim() 
            
            res_text = "VICTORY!" if not check_for_game_over() else "DEFEAT!"
            col = (0, 255, 0) if not check_for_game_over() else (255, 0, 0)
            
            pulse_scale = 1.0 + 0.1 * math.sin(time.time() * 8)
            
            base_surf = HUGE_FONT.render(res_text, True, col)
            new_w = int(base_surf.get_width() * pulse_scale)
            new_h = int(base_surf.get_height() * pulse_scale)
            res_surf = pygame.transform.scale(base_surf, (new_w, new_h))
            
            res_rect = res_surf.get_rect(center=(120, 120))
            canvas.blit(res_surf, res_rect)
            
            if show_blink:
                reset_surf = FONT.render("Tap to Reset", True, LINE_COLOR)
                reset_rect = reset_surf.get_rect(center=(120, 220))
                canvas.blit(reset_surf, reset_rect)

    if DISPLAY_MESSAGE and (game_state == "WAITING" or time.time() < MESSAGE_DISPLAY_TIME):
        draw_text(canvas, DISPLAY_MESSAGE[:30], (10, 5), TEXT_COLOR)

    update_and_draw_vfx(canvas)

    rotated_canvas = pygame.transform.rotate(canvas, 90)
    
    shake_offset = (0, 0)
    if time.time() < shake_end_time:
        offset_x = random.randint(-SHAKE_INTENSITY, SHAKE_INTENSITY)
        offset_y = random.randint(-SHAKE_INTENSITY, SHAKE_INTENSITY)
        shake_offset = (offset_x, offset_y)
        
    screen.blit(rotated_canvas, shake_offset)
    pygame.display.flip()

def waiting_state():
    global game_state, is_connected, handshake_sent, handshake_complete, DISPLAY_MESSAGE

    if status == Status.CONNECTED:
        if not handshake_sent:
            print("GAME: Connected. Sending Handshake HELLO...")
            send_data({"type": "HELLO"})
            handshake_sent = True
            DISPLAY_MESSAGE = "Syncing..."
        
        data = receive_data()
        if data and data.get("type") == "HELLO":
            print("GAME: Handshake Received! Sync Complete.")
            handshake_complete = True
            
        if handshake_complete:
            is_connected = True
            game_state = "PLACING_SHIPS"
            DISPLAY_MESSAGE = "" 
            print("GAME: Moving to PLACING_SHIPS")

def placing_ships_state():
    global done_placing_ships, shooting_cursor_pos, ship_positions, opponent_ready
    global current_ship_length, current_ship_orientation, ship_placement_index
    global occupied_placement, DISPLAY_MESSAGE, MESSAGE_DISPLAY_TIME
    
    if not done_placing_ships:
        x, y = shooting_cursor_pos
        if not GPIO.input(BUTTON_RIGHT):
            shooting_cursor_pos = ((x + 1) % GRID_SIZE, y) 
            time.sleep(0.2)
        elif not GPIO.input(BUTTON_DOWN):
            shooting_cursor_pos = (x, (y + 1) % GRID_SIZE)
            time.sleep(0.2)
        elif not GPIO.input(BUTTON_ROTATE):
            current_ship_orientation = "vertical" if current_ship_orientation == "horizontal" else "horizontal"
            time.sleep(0.2)
        elif not GPIO.input(BUTTON_SELECT):
            ship_coords = get_ship_positions(shooting_cursor_pos, current_ship_length, current_ship_orientation)
            if in_bounds(ship_coords) and not ship_overlaps(ship_coords, occupied_placement):
                new_ship_name = f"ship_{ship_placement_index}"
                ship_positions[new_ship_name] = {"parts": [{"pos": pos, "hit": False} for pos in ship_coords], "sunk": False}
                occupied_placement.update(ship_coords)
                ship_placement_index += 1
                if ship_placement_index < len(SHIPS_TO_PLACE):
                    current_ship_length = SHIPS_TO_PLACE[ship_placement_index]
                    current_ship_orientation = "horizontal" 
                else:
                    done_placing_ships = True
                    DISPLAY_MESSAGE = "Waiting for opponent..."
                    MESSAGE_DISPLAY_TIME = time.time() + 2.0 
                    send_data({"type": "SHIPS_PLACED"})
            else:
                DISPLAY_MESSAGE = "Invalid placement."
                MESSAGE_DISPLAY_TIME = time.time() + 1.5
            time.sleep(0.3)
    elif done_placing_ships and not opponent_ready:
        data = receive_data()
        if data and data.get("type") == "SHIPS_PLACED":
            opponent_ready = True
            DISPLAY_MESSAGE = "Opponent Ready!"
            MESSAGE_DISPLAY_TIME = time.time() + 2.0 

def deciding_first_turn_state():
    global first_turn_started, first_turn_decided, has_first_turn, DISPLAY_MESSAGE, MESSAGE_DISPLAY_TIME, waiting_for_opponent_ready
    
    if not first_turn_started:
        first_turn_started = True
        waiting_for_opponent_ready = True
        send_data({"type": "READY_TO_START"})
        return
    
    if waiting_for_opponent_ready:
        data = receive_data()
        if data and data.get("type") == "READY_TO_START":
            waiting_for_opponent_ready = False
            if IS_MASTER_PI:
                has_first_turn = True
                DISPLAY_MESSAGE = "You go first!"
            else:
                has_first_turn = False
                DISPLAY_MESSAGE = "Opponent goes first."
            MESSAGE_DISPLAY_TIME = time.time() + 2.0
            first_turn_decided = True

def shooting_state():
    global shot_fired, shooting_result_received, shooting_cursor_pos, shots_fired
    global DISPLAY_MESSAGE, MESSAGE_DISPLAY_TIME, game_over, last_sent_shot, result_display_time
    
    if not shot_fired:
        x, y = shooting_cursor_pos
        if not GPIO.input(BUTTON_RIGHT):
            shooting_cursor_pos = ((x + 1) % GRID_SIZE, y) 
            time.sleep(0.2)
        elif not GPIO.input(BUTTON_DOWN):
            shooting_cursor_pos = (x, (y + 1) % GRID_SIZE)
            time.sleep(0.2)
        elif not GPIO.input(BUTTON_SELECT):
            target_pos = (x, y)
            if target_pos not in shots_fired:
                shot_data = {"type": "SHOT", "coord": target_pos}
                if send_data(shot_data):
                    shots_fired[target_pos] = None 
                    last_sent_shot = target_pos
                    shot_fired = True
                    DISPLAY_MESSAGE = "Firing..."
                    MESSAGE_DISPLAY_TIME = time.time() + 1.5
            else:
                DISPLAY_MESSAGE = "Already shot there!"
                MESSAGE_DISPLAY_TIME = time.time() + 1.5
            time.sleep(0.3)
    elif shot_fired and not shooting_result_received:
        data = receive_data()
        if data and data.get("type") == "SHOT_RESULT":
            shooting_result = data.get("result")
            coord = tuple(data.get("coord"))
            if coord == last_sent_shot:
                shots_fired[coord] = shooting_result 
                
                display_text = shooting_result.replace("_", " ")
                
                DISPLAY_MESSAGE = f"{display_text}!"
                MESSAGE_DISPLAY_TIME = time.time() + 2.0 
                result_display_time = time.time() + 2.0 
                
                trigger_explosion(coord, display_text + "!")

                if shooting_result == "ALL_SUNK": game_over = True
                shooting_result_received = True

def receiving_state():
    global shooting_result_sent, game_over, my_board_shots, processed_shot_coords
    global DISPLAY_MESSAGE, MESSAGE_DISPLAY_TIME, result_display_time
    
    if shooting_result_sent: return 
    
    enemy_shot_data = receive_data()
    if enemy_shot_data and enemy_shot_data.get("type") == "SHOT":
        enemy_shot_coord = tuple(enemy_shot_data.get("coord"))
        if enemy_shot_coord in processed_shot_coords: return 
        processed_shot_coords.add(enemy_shot_coord)
        
        result = "MISS"
        is_hit = False
        for ship_name, ship_data in ship_positions.items():
            if ship_data["sunk"]: continue
            for part in ship_data["parts"]:
                if part["pos"] == enemy_shot_coord and not part["hit"]:
                    part["hit"] = True; is_hit = True
                    if check_if_sunk(ship_data["parts"]):
                        ship_data["sunk"] = True; result = "SUNK"
                        if check_for_game_over(): game_over = True; result = "ALL_SUNK"
                        break
                    else: result = "HIT"
                    break
            if is_hit: break
        
        my_board_shots[enemy_shot_coord] = result 
        
        display_text = result.replace("_", " ")
        
        DISPLAY_MESSAGE = f"Enemy: {display_text}"
        MESSAGE_DISPLAY_TIME = time.time() + 2.0 
        
        if result == "MISS":
             trigger_explosion(enemy_shot_coord, "MISS!")
        elif result in ["HIT", "SUNK", "ALL_SUNK"]:
             trigger_explosion(enemy_shot_coord, "HIT!")

        response_data = {"type": "SHOT_RESULT", "coord": enemy_shot_coord, "result": result}
        if send_data(response_data):
            shooting_result_sent = True
            result_display_time = time.time() + 2.0

def end_state():
    pass

def next_state():
    global game_state, is_connected, done_placing_ships, first_turn_decided, has_first_turn
    global shooting_result_received, shooting_result_sent, game_over, shot_fired, opponent_ready
    global waiting_for_opponent_ready, result_display_time
    
    if DISPLAY_MESSAGE and time.time() < MESSAGE_DISPLAY_TIME:
        if game_state not in ["RECEIVING", "SHOOTING", "DECIDING_FIRST_TURN", "PLACING_SHIPS", "WAITING"]: return

    if game_state == "WAITING":
        pass
    elif game_state == "PLACING_SHIPS":
        if done_placing_ships and opponent_ready: 
            game_state = "DECIDING_FIRST_TURN"
            done_placing_ships = False; opponent_ready = False
    elif game_state == "DECIDING_FIRST_TURN":
        if first_turn_decided and has_first_turn:
            game_state = "SHOOTING"
            first_turn_decided = False; waiting_for_opponent_ready = False
        elif first_turn_decided:
            game_state = "RECEIVING"
            first_turn_decided = False; waiting_for_opponent_ready = False
    elif game_state == "SHOOTING":
        if shot_fired and shooting_result_received and time.time() >= result_display_time:
            if game_over: game_state = "END"
            else: game_state = "RECEIVING"
            shot_fired = False; shooting_result_received = False; result_display_time = 0
    elif game_state == "RECEIVING":
        if shooting_result_sent and time.time() >= result_display_time:
            if game_over: game_state = "END"; shooting_result_sent = False
            else: game_state = "SHOOTING"; shooting_result_sent = False
            result_display_time = 0
    elif game_state == "END":
        pass

def perform_state():
    if game_state == "START_SCREEN": pass
    elif game_state == "WAITING": waiting_state() 
    elif game_state == "PLACING_SHIPS": placing_ships_state()
    elif game_state == "DECIDING_FIRST_TURN": deciding_first_turn_state()
    elif game_state == "SHOOTING": shooting_state()
    elif game_state == "RECEIVING": receiving_state()
    elif game_state == "END": end_state()

def run_glib_loop():
    try: GLib.MainLoop().run()
    except Exception: pass

def main():
    global reset_needed, game_state, running
    
    register_agent()
    
    threading.Thread(target=nfc_pipe_watcher, daemon=True).start()
    threading.Thread(target=rfcomm_server, daemon=True).start()
    threading.Thread(target=rfcomm_client, daemon=True).start()
    threading.Thread(target=tx_queue_worker, daemon=True).start()
    threading.Thread(target=run_glib_loop, daemon=True).start()
    
    try:
        while running: 
            pitft.update() 
            
            for event in pygame.event.get():
                if event.type == pygame.QUIT: running = False
                if event.type == KEYDOWN and event.key == K_ESCAPE: running = False
                
                if event.type == MOUSEBUTTONUP:
                    x, y = pygame.mouse.get_pos()
                    print(f"Touch Detected at: {x}, {y}")
                    
                    if game_state == "START_SCREEN":
                        if y > 160 and x > 270:
                            print("USER: Touch Quit")
                            running = False 
                        else:
                            print("USER: Touch Start - OPENING CONNECTION GATE")
                            connection_enabled.set()
                            game_state = "WAITING"
                    
                    elif game_state == "END":
                         print("USER: Tap to Reset Game")
                         reset_game_state()

            check_quit_button()
            
            if reset_needed:
                reset_game_state()
            
            next_state()
            perform_state()
            update_screen()
            CLOCK.tick(60)
            
    except KeyboardInterrupt:
        print("\nKeyboard interrupt received...")
    except Exception as e:
        print(f"Unexpected error: {e}")
    finally:
        print("Cleaning up and exiting...")
        
        try:
            if rfcomm_sock: 
                rfcomm_sock.shutdown(socket.SHUT_RDWR)
                rfcomm_sock.close()
        except: pass
        
        try:
            if client_sock:
                client_sock.shutdown(socket.SHUT_RDWR)
                client_sock.close()
        except: pass
        
        try:
            if server_sock: 
                server_sock.close()
        except: pass
        
        try: pygame.quit()
        except: pass
        
        try: GPIO.cleanup()
        except: pass
        
        os._exit(0)

if __name__ == "__main__":
    main()