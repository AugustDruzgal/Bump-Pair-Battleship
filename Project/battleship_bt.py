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
import json
import time
import queue
import RPi.GPIO as GPIO
from enum import Enum

# --- SMART DISPLAY SETUP ---
if os.path.exists('/dev/fb1'):
    os.environ["SDL_FBDEV"] = "/dev/fb1"
else:
    os.environ["SDL_FBDEV"] = "/dev/fb0"

if not os.environ.get('DISPLAY'):
    os.environ["SDL_VIDEODRIVER"] = "fbcon"
    os.environ["SDL_MOUSEDRV"] = "dummy"
    os.environ["SDL_MOUSEDEV"] = "/dev/null"

# --- GPIO SETUP ---
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

# --- GLOBAL NETWORKING VARS ---
AGENT_PATH = "/test/agent"
rfcomm_sock = None
server_sock = None
client_sock = None
mode = Mode.NONE
status = Status.DISCONNECTED

my_addr = bluetooth.read_local_bdaddr()[0]
target_addr = None 

# --- DBUS AGENT ---
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

# --- SERVER THREAD ---
def rfcomm_server():
    global mode, status, server_sock, rfcomm_sock
    print("SERVER: Starting...")
    while True:
        try:
            server_sock = bluetooth.BluetoothSocket(bluetooth.RFCOMM)
            server_sock.bind(("", 1)) 
            server_sock.listen(1)
            print("SERVER: Waiting for connection on Port 1...")
            rfcomm_sock, addr = server_sock.accept()
            print(f"SERVER: Connection accepted from {addr}")
            mode = Mode.SERVER
            status = Status.CONNECTED
            buffer = ""
            while True:
                data = rfcomm_sock.recv(1024)
                if not data: break
                buffer += data.decode('utf-8')
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if line: rx_queue.put(line.strip())
        except Exception as e:
            print(f"SERVER ERROR: {e}")
        finally:
            print("SERVER: Connection lost. Resetting...")
            status = Status.DISCONNECTED
            try: server_sock.close()
            except: pass
            try: rfcomm_sock.close()
            except: pass
            time.sleep(1)

# --- CLIENT THREAD ---
def rfcomm_client():
    global mode, status, client_sock, target_addr
    print(f"CLIENT: Startup. Target is {target_addr}")
    while True:
        try:
            status = Status.CONNECTING
            client_sock = bluetooth.BluetoothSocket(bluetooth.RFCOMM)
            print(f"CLIENT: Connecting to {target_addr} port 1...")
            client_sock.connect((target_addr, 1))
            print("CLIENT: Connected!")
            mode = Mode.CLIENT
            status = Status.CONNECTED
            buffer = ""
            while True:
                data = client_sock.recv(1024)
                if not data: break
                buffer += data.decode('utf-8')
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if line: rx_queue.put(line.strip())
        except Exception as e:
            print(f"CLIENT: Connect failed ({e}). Retrying in 2s...")
            status = Status.DISCONNECTED
            if client_sock:
                try: client_sock.close()
                except: pass
            time.sleep(2)

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

def check_quit_button():
    global quit_press_start
    if not GPIO.input(BUTTON_SELECT):
        if quit_press_start is None:
            quit_press_start = time.time()
        else:
            if time.time() - quit_press_start >= QUIT_HOLD_TIME:
                pygame.quit()
                GPIO.cleanup()
                sys.exit()
    else:
        quit_press_start = None

# --- UI LAYOUT CONSTANTS (PORTRAIT 240x320) ---
IS_MASTER_PI = False 
GRID_SIZE = 5
CELL_SIZE = 40  # 200px Grid

GRID_OFFSET_X = 20 
GRID_OFFSET_Y = 60 

LINE_COLOR = (255, 255, 255) 
WATER_COLOR = (0, 0, 0) 
SHIP_COLOR = (150, 150, 150) 
CURSOR_COLOR = (0, 255, 0) 
MISS_COLOR = (200, 200, 200) 
HIT_COLOR = (255, 0, 0)
INVALID_COLOR = (255, 100, 0)
TEXT_COLOR = (255, 255, 0)
ICON_COLOR = (180, 180, 180) # Slightly dimmed for icons

game_state = "WAITING"
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

pygame.init()
pygame.mouse.set_visible(False)
screen = pygame.display.set_mode((320, 240))
pygame.display.set_caption("Battleship")
FONT = pygame.font.Font(None, 25)
SMALL_FONT = pygame.font.Font(None, 20)
CLOCK = pygame.time.Clock()

def send_data(data):
    global message_sequence
    try:
        data['seq'] = message_sequence; message_sequence += 1
        tx_queue.put(json.dumps(data))
        return True
    except Exception: return False

def receive_data():
    try:
        return json.loads(rx_queue.get(block=False))
    except queue.Empty:
        return None
    except Exception:
        return None

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

def draw_text(surface, text, pos, color=LINE_COLOR):
    text_surface = SMALL_FONT.render(text, True, color)
    surface.blit(text_surface, pos)

# --- ICON DRAWING HELPER ---
def draw_icon(surface, shape, center_pos, size=10, color=ICON_COLOR):
    x, y = center_pos
    if shape == "RIGHT_ARROW":
        # Triangle pointing right
        points = [(x - size//2, y - size//2), (x - size//2, y + size//2), (x + size//2, y)]
        pygame.draw.polygon(surface, color, points)
    elif shape == "DOWN_ARROW":
        # Triangle pointing down
        points = [(x - size//2, y - size//2), (x + size//2, y - size//2), (x, y + size//2)]
        pygame.draw.polygon(surface, color, points)

def draw_grid(is_shooting_board, cursor_pos=None, temp_ship_positions=None):
    canvas = pygame.Surface((240, 320))
    canvas.fill(WATER_COLOR)

    for x in range(GRID_SIZE):
        for y in range(GRID_SIZE):
            rect = pygame.Rect(GRID_OFFSET_X + x * CELL_SIZE, GRID_OFFSET_Y + y * CELL_SIZE, CELL_SIZE, CELL_SIZE)
            pygame.draw.rect(canvas, LINE_COLOR, rect, 1) 
            coord = (x, y)
            
            if not is_shooting_board:
                ship_part = get_ship_part_at(coord)
                if ship_part:
                    color = SHIP_COLOR
                    if ship_part['hit']: color = (100, 0, 0) 
                    pygame.draw.rect(canvas, color, rect, 0) 
                    pygame.draw.rect(canvas, LINE_COLOR, rect, 1) 
                    if ship_part['hit']: draw_marker(canvas, coord, HIT_COLOR)
                elif temp_ship_positions and coord in temp_ship_positions:
                    ship_coords = get_ship_positions(shooting_cursor_pos, current_ship_length, current_ship_orientation)
                    is_valid = in_bounds(ship_coords) and not ship_overlaps(ship_coords, occupied_placement)
                    fill_color = SHIP_COLOR if is_valid else INVALID_COLOR
                    pygame.draw.rect(canvas, fill_color, rect, 0)
                    pygame.draw.rect(canvas, LINE_COLOR, rect, 1)
            
            if is_shooting_board and coord in shots_fired:
                result = shots_fired[coord]
                if result == "MISS": draw_marker(canvas, coord, MISS_COLOR)
                elif result in ["HIT", "SUNK", "ALL_SUNK"]: draw_marker(canvas, coord, HIT_COLOR)
            elif not is_shooting_board and coord in my_board_shots and my_board_shots[coord] == "MISS":
                draw_marker(canvas, coord, MISS_COLOR)
    
    if cursor_pos:
        rect = pygame.Rect(GRID_OFFSET_X + cursor_pos[0] * CELL_SIZE, GRID_OFFSET_Y + cursor_pos[1] * CELL_SIZE, CELL_SIZE, CELL_SIZE)
        pygame.draw.rect(canvas, CURSOR_COLOR, rect, 3)

    return canvas

def draw_marker(surface, coord, color):
    center_x = GRID_OFFSET_X + coord[0] * CELL_SIZE + CELL_SIZE // 2
    center_y = GRID_OFFSET_Y + coord[1] * CELL_SIZE + CELL_SIZE // 2
    radius = CELL_SIZE // 4
    pygame.draw.circle(surface, color, (center_x, center_y), radius)

def update_screen():
    global screen, DISPLAY_MESSAGE, MESSAGE_DISPLAY_TIME
    
    temp_ship_positions = None
    if game_state == "PLACING_SHIPS" and not done_placing_ships:
        temp_ship_positions = get_ship_positions(shooting_cursor_pos, current_ship_length, current_ship_orientation)
    
    # --- RENDER LOGIC ---
    if game_state == "PLACING_SHIPS":
        canvas = draw_grid(False, shooting_cursor_pos, temp_ship_positions)
        status_text = f"Setup: {current_ship_length} ({current_ship_orientation[0].upper()})"
        draw_text(canvas, status_text, (60, 20), LINE_COLOR)
        
        # Show ALL buttons
        draw_icon(canvas, "RIGHT_ARROW", (40, 295)) # Pos 1
        draw_icon(canvas, "DOWN_ARROW", (90, 295))  # Pos 2
        draw_text(canvas, "Rot", (130, 288), ICON_COLOR) # Pos 3
        draw_text(canvas, "Sel", (190, 288), ICON_COLOR) # Pos 4

    elif game_state == "SHOOTING":
        canvas = draw_grid(True, shooting_cursor_pos if not shot_fired else None)
        draw_text(canvas, "SHOOTING", (80, 20), LINE_COLOR)
        
        # Show buttons MINUS Rot (keep positions fixed)
        draw_icon(canvas, "RIGHT_ARROW", (40, 295)) # Pos 1
        draw_icon(canvas, "DOWN_ARROW", (90, 295))  # Pos 2
        # Pos 3 (Rot) is empty space
        draw_text(canvas, "Sel", (190, 288), ICON_COLOR) # Pos 4

    elif game_state == "RECEIVING":
        canvas = draw_grid(False)
        draw_text(canvas, "RECEIVING", (80, 20), LINE_COLOR)
        # NO BUTTONS SHOWN (Passive state)

    else:
        # Menus / Waiting / End
        canvas = pygame.Surface((240, 320))
        canvas.fill(WATER_COLOR)
        if game_state == "WAITING": draw_text(canvas, "Connecting...", (70, 150), LINE_COLOR)
        elif game_state == "DECIDING_FIRST_TURN": draw_text(canvas, "Deciding Turn...", (60, 150), LINE_COLOR)
        elif game_state == "END": 
            res = "VICTORY!" if not check_for_game_over() else "DEFEAT!"
            col = (0, 255, 0) if not check_for_game_over() else (255, 0, 0)
            draw_text(canvas, res, (80, 150), col)

    if DISPLAY_MESSAGE and time.time() < MESSAGE_DISPLAY_TIME:
        draw_text(canvas, DISPLAY_MESSAGE[:30], (10, 5), TEXT_COLOR)

    # --- FINAL ROTATION ---
    rotated_canvas = pygame.transform.rotate(canvas, 90)
    screen.blit(rotated_canvas, (0, 0))
    pygame.display.flip()

# --- GAME STATES ---

def waiting_state():
    global game_state, is_connected
    if status == Status.CONNECTED:
        is_connected = True
        game_state = "PLACING_SHIPS"
        print("GAME: Connected! Moving to PLACING_SHIPS")

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
                    MESSAGE_DISPLAY_TIME = time.time() + 5.0 
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
                    MESSAGE_DISPLAY_TIME = time.time() + 1.0
            else:
                DISPLAY_MESSAGE = "Already shot there!"
                MESSAGE_DISPLAY_TIME = time.time() + 1.0
            time.sleep(0.3)

    elif shot_fired and not shooting_result_received:
        data = receive_data()
        if data and data.get("type") == "SHOT_RESULT":
            shooting_result = data.get("result")
            coord = tuple(data.get("coord"))
            if coord == last_sent_shot:
                shots_fired[coord] = shooting_result 
                DISPLAY_MESSAGE = f"{shooting_result}!"
                MESSAGE_DISPLAY_TIME = time.time() + 3.0 
                result_display_time = time.time() + 3.0 
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
        DISPLAY_MESSAGE = f"Enemy: {result}"
        MESSAGE_DISPLAY_TIME = time.time() + 3.0 
        
        response_data = {"type": "SHOT_RESULT", "coord": enemy_shot_coord, "result": result}
        if send_data(response_data):
            shooting_result_sent = True
            result_display_time = time.time() + 3.0

def end_state():
    if not GPIO.input(BUTTON_SELECT):
        pygame.quit(); sys.exit()

def next_state():
    global game_state, is_connected, done_placing_ships, first_turn_decided, has_first_turn
    global shooting_result_received, shooting_result_sent, game_over, shot_fired, opponent_ready
    global waiting_for_opponent_ready, result_display_time
    
    if DISPLAY_MESSAGE and time.time() < MESSAGE_DISPLAY_TIME:
        if game_state not in ["RECEIVING", "SHOOTING", "DECIDING_FIRST_TURN", "PLACING_SHIPS"]: return

    if game_state == "WAITING":
        if is_connected: game_state = "PLACING_SHIPS"
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
    if game_state == "WAITING": waiting_state() 
    elif game_state == "PLACING_SHIPS": placing_ships_state()
    elif game_state == "DECIDING_FIRST_TURN": deciding_first_turn_state()
    elif game_state == "SHOOTING": shooting_state()
    elif game_state == "RECEIVING": receiving_state()
    elif game_state == "END": end_state()

def run_glib_loop():
    try: GLib.MainLoop().run()
    except Exception: pass

def main():
    global IS_MASTER_PI, target_addr

    # --- HARDCODED ADDRESSES ---
    ADDR_DEVICE_1 = "DC:A6:32:B4:13:71" 
    ADDR_DEVICE_2 = "D8:3A:DD:3E:B3:A1" 
    
    my_addr_norm = my_addr.upper()
    role = 'S' # Default

    # --- ROLE DETERMINATION ---
    if my_addr_norm == ADDR_DEVICE_1:
        print("IDENTITY: I am DEVICE 1 (Server)")
        role = 'S'
        IS_MASTER_PI = True 
    elif my_addr_norm == ADDR_DEVICE_2:
        print("IDENTITY: I am DEVICE 2 (Client)")
        role = 'C'
        IS_MASTER_PI = False
        target_addr = ADDR_DEVICE_1 # Device 2 connects to Device 1
    else:
        print("IDENTITY: Unknown MAC, defaulting to Server")
        role = 'S'
        IS_MASTER_PI = True

    register_agent()

    # --- START NETWORKING THREADS ---
    if role == 'S':
        t = threading.Thread(target=rfcomm_server, daemon=True)
        t.start()
    else:
        t = threading.Thread(target=rfcomm_client, daemon=True)
        t.start()

    threading.Thread(target=tx_queue_worker, daemon=True).start()
    threading.Thread(target=run_glib_loop, daemon=True).start()

    # --- MAIN GAME LOOP ---
    try:
        while True:             
            # pitft.update() # REMOVED
            check_quit_button()
            
            for event in pygame.event.get():
                if event.type == pygame.QUIT: pygame.quit(); sys.exit()
                if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE: pygame.quit(); sys.exit()

            next_state()
            perform_state()
            update_screen()
            CLOCK.tick(60)
            
    except KeyboardInterrupt:
        pygame.quit(); sys.exit()
    finally:
        GPIO.cleanup()

if __name__ == "__main__":
    try: main()
    finally: GPIO.cleanup()