import pygame  # type:ignore
import os
import sys
import json
import time
import platform

# --- Global Constants & Variables ---
PIPE_OUT = "" 
PIPE_IN = ""  
IS_MASTER_PI = False 

GRID_SIZE = 5
SCREEN_WIDTH = 240
SCREEN_HEIGHT = 320
GRID_OFFSET_X = 10
GRID_OFFSET_Y = 60
CELL_SIZE = 40 

LINE_COLOR = (255, 255, 255) 
WATER_COLOR = (0, 0, 0) 
SHIP_COLOR = (150, 150, 150) 
CURSOR_COLOR = (0, 255, 0) 
MISS_COLOR = (200, 200, 200) 
HIT_COLOR = (255, 0, 0)
INVALID_COLOR = (255, 100, 0)

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

# Message tracking to prevent duplicates
processed_shot_coords = set()
last_sent_shot = None
message_sequence = 0

# First turn decision tracking
waiting_for_opponent_ready = False

pygame.init()
pygame.font.init()
screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
pygame.display.set_caption("Battleship Pi Test")
FONT = pygame.font.Font(None, 15)
CLOCK = pygame.time.Clock()

# --- Network / Pipe Functions ---

def send_data(data):
    """Send data through pipe with error handling"""
    global message_sequence
    try:
        data['seq'] = message_sequence
        message_sequence += 1
        
        with open(PIPE_OUT, 'w') as pipe_out:
            message = json.dumps(data)
            pipe_out.write(message + '\n')
            pipe_out.flush()
        return True
    except Exception as e:
        print(f"Send error: {e}")
        return False

def receive_data(blocking=False, timeout=0):
    """
    Cross-platform receive data with configurable blocking behavior
    """
    try:
        if platform.system() == 'Windows':
            if blocking:
                start_time = time.time()
                while True:
                    try:
                        with open(PIPE_IN, 'r') as pipe_in:
                            line = pipe_in.readline().strip()
                            if line: return json.loads(line)
                    except (FileNotFoundError, PermissionError): pass
                    if timeout > 0 and (time.time() - start_time) > timeout: return None
                    time.sleep(0.01)
            else:
                try:
                    if os.path.exists(PIPE_IN):
                        with open(PIPE_IN, 'r') as pipe_in:
                            line = pipe_in.readline().strip()
                            if line: return json.loads(line)
                except (FileNotFoundError, PermissionError, ValueError): pass
                return None
        else:
            import select
            if blocking:
                fd = os.open(PIPE_IN, os.O_RDONLY)
                ready, _, _ = select.select([fd], [], [], timeout if timeout > 0 else None)
                if not ready: os.close(fd); return None
                data_bytes = os.read(fd, 4096)
                os.close(fd)
            else:
                try:
                    fd = os.open(PIPE_IN, os.O_RDONLY | os.O_NONBLOCK)
                    ready, _, _ = select.select([fd], [], [], 0)
                    if not ready: os.close(fd); return None
                    data_bytes = os.read(fd, 4096)
                    os.close(fd)
                except OSError: return None
            
            if not data_bytes: return None
            line = data_bytes.decode('utf-8').strip()
            if line:
                lines = line.split('\n')
                return json.loads(lines[0])
            return None
    except Exception as e:
        if blocking: print(f"Receive error: {e}")
        return None

# --- Game Logic Helpers ---

def get_ship_positions(start, length, orientation):
    x, y = start
    if orientation == "horizontal": return [(x + i, y) for i in range(length)]
    else: return [(x, y + i) for i in range(length)]

def in_bounds(positions, grid_width, grid_height):
    return all(0 <= x < grid_width and 0 <= y < grid_height for x, y in positions)

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

# --- Drawing Functions ---

def draw_grid(is_shooting_board, cursor_pos=None, temp_ship_positions=None):
    global ship_positions
    screen.fill(WATER_COLOR)
    for x in range(GRID_SIZE):
        for y in range(GRID_SIZE):
            rect = pygame.Rect(GRID_OFFSET_X + x * CELL_SIZE, GRID_OFFSET_Y + y * CELL_SIZE, CELL_SIZE, CELL_SIZE)
            pygame.draw.rect(screen, LINE_COLOR, rect, 1) 
            coord = (x, y)
            
            if not is_shooting_board:
                ship_part = get_ship_part_at(coord)
                if ship_part:
                    color = SHIP_COLOR
                    if ship_part['hit']: color = (100, 0, 0) 
                    pygame.draw.rect(screen, color, rect, 0) 
                    pygame.draw.rect(screen, LINE_COLOR, rect, 1) 
                    if ship_part['hit']: draw_marker(coord, HIT_COLOR)
                elif temp_ship_positions and coord in temp_ship_positions:
                    ship_coords = get_ship_positions(shooting_cursor_pos, current_ship_length, current_ship_orientation)
                    is_valid = in_bounds(ship_coords, GRID_SIZE, GRID_SIZE) and not ship_overlaps(ship_coords, occupied_placement)
                    fill_color = SHIP_COLOR if is_valid else INVALID_COLOR
                    pygame.draw.rect(screen, fill_color, rect, 0)
                    pygame.draw.rect(screen, LINE_COLOR, rect, 1)
            
            if is_shooting_board and coord in shots_fired:
                result = shots_fired[coord]
                if result == "MISS": draw_marker(coord, MISS_COLOR)
                elif result in ["HIT", "SUNK", "ALL_SUNK"]: draw_marker(coord, HIT_COLOR)
            elif not is_shooting_board and coord in my_board_shots and my_board_shots[coord] == "MISS":
                draw_marker(coord, MISS_COLOR)
    
    if cursor_pos:
        rect = pygame.Rect(GRID_OFFSET_X + cursor_pos[0] * CELL_SIZE, GRID_OFFSET_Y + cursor_pos[1] * CELL_SIZE, CELL_SIZE, CELL_SIZE)
        pygame.draw.rect(screen, CURSOR_COLOR, rect, 3)

def draw_marker(coord, color):
    center_x = GRID_OFFSET_X + coord[0] * CELL_SIZE + CELL_SIZE // 2
    center_y = GRID_OFFSET_Y + coord[1] * CELL_SIZE + CELL_SIZE // 2
    radius = CELL_SIZE // 4
    pygame.draw.circle(screen, color, (center_x, center_y), radius)

def draw_text(text, position, color=(255, 255, 255)):
    text_surface = FONT.render(text, True, color)
    screen.blit(text_surface, position)

def update_screen():
    global screen, DISPLAY_MESSAGE, MESSAGE_DISPLAY_TIME
    screen.fill((0, 0, 0)) 
    
    temp_ship_positions = None
    if game_state == "PLACING_SHIPS" and not done_placing_ships:
        temp_ship_positions = get_ship_positions(shooting_cursor_pos, current_ship_length, current_ship_orientation)
    
    if game_state == "PLACING_SHIPS":
        status_text = f"Place Ship ({current_ship_length}): {current_ship_orientation[0].upper()}"
        draw_text(status_text, (GRID_OFFSET_X, 10))
        draw_grid(False, shooting_cursor_pos, temp_ship_positions) 
    elif game_state == "SHOOTING":
        draw_text("ATTACK (Enemy Board)", (GRID_OFFSET_X, 10))
        cursor = shooting_cursor_pos if not shot_fired else None
        draw_grid(True, cursor) 
    elif game_state == "RECEIVING":
        draw_text("DEFEND (Your Board)", (GRID_OFFSET_X, 10))
        draw_grid(False) 
    elif game_state == "WAITING":
        draw_text("Waiting for connection...", (10, 100))
    elif game_state == "DECIDING_FIRST_TURN":
        draw_text("Deciding first turn...", (10, 100))
    elif game_state == "END":
        result_text = "VICTORY!" if not check_for_game_over() else "DEFEAT!"
        draw_text(result_text, (10, 100), (0, 255, 0) if not check_for_game_over() else (255, 0, 0))
    
    if DISPLAY_MESSAGE and time.time() < MESSAGE_DISPLAY_TIME:
        draw_text(DISPLAY_MESSAGE, (10, SCREEN_HEIGHT - 30), (255, 255, 0))
    pygame.display.flip()

# --- State Functions ---

def waiting_state():
    global is_connected, IS_MASTER_PI, PIPE_OUT, PIPE_IN
    
    choice = input("Is this instance 'MASTER' (m) or 'SLAVE' (s)? (M/S): ").strip().upper()
    if choice == 'M':
        IS_MASTER_PI = True
        local_role = 'MASTER'
        PIPE_OUT = 'pi1_to_pi2'
        PIPE_IN = 'pi2_to_pi1'
    elif choice == 'S':
        IS_MASTER_PI = False
        local_role = 'SLAVE'
        PIPE_OUT = 'pi2_to_pi1'
        PIPE_IN = 'pi1_to_pi2'
    else:
        IS_MASTER_PI = False
        local_role = 'SLAVE'
        PIPE_OUT = 'pi2_to_pi1'
        PIPE_IN = 'pi1_to_pi2'
    
    print(f"Set up as {local_role}. Performing connection handshake...")
    send_data({"type": "ROLE_ANNOUNCEMENT", "role": local_role})
    
    opponent_role = None
    timeout = time.time() + 10
    while opponent_role is None and time.time() < timeout:
        for event in pygame.event.get():
            if event.type == pygame.QUIT: pygame.quit(); sys.exit()
        update_screen()
        CLOCK.tick(60)
        data = receive_data(blocking=True, timeout=0.1)
        if data and data.get("type") == "ROLE_ANNOUNCEMENT":
            opponent_role = data["role"]
            break
    
    if opponent_role is None:
        print("Connection timeout. Exiting.")
        pygame.quit(); sys.exit()
    
    if (local_role == 'MASTER' and opponent_role == 'SLAVE') or \
       (local_role == 'SLAVE' and opponent_role == 'MASTER'):
        is_connected = True
        print("Connection verified: Roles complement each other.")
    else:
        print(f"Connection failed: Local ({local_role}) vs Opponent ({opponent_role}). Exiting.")
        pygame.quit(); sys.exit()

def placing_ships_state():
    global done_placing_ships, shooting_cursor_pos, ship_positions, opponent_ready
    global current_ship_length, current_ship_orientation, ship_placement_index
    global occupied_placement, DISPLAY_MESSAGE, MESSAGE_DISPLAY_TIME
    grid_w, grid_h = GRID_SIZE, GRID_SIZE
    
    if not done_placing_ships:
        for event in pygame.event.get():
            if event.type == pygame.QUIT: pygame.quit(); sys.exit()
            if event.type == pygame.KEYDOWN:
                x, y = shooting_cursor_pos
                if event.key == pygame.K_RIGHT: x = (x + 1) % grid_w
                elif event.key == pygame.K_DOWN: y = (y + 1) % grid_h
                elif event.key == pygame.K_LEFT: x = (x - 1 + grid_w) % grid_w 
                elif event.key == pygame.K_UP: y = (y - 1 + grid_h) % grid_h 
                elif event.key == pygame.K_r: current_ship_orientation = "vertical" if current_ship_orientation == "horizontal" else "horizontal"
                elif event.key == pygame.K_SPACE: 
                    ship_coords = get_ship_positions(shooting_cursor_pos, current_ship_length, current_ship_orientation)
                    if in_bounds(ship_coords, grid_w, grid_h) and not ship_overlaps(ship_coords, occupied_placement):
                        new_ship_name = f"ship_{ship_placement_index}"
                        ship_positions[new_ship_name] = {"parts": [{"pos": pos, "hit": False} for pos in ship_coords], "sunk": False}
                        occupied_placement.update(ship_coords)
                        ship_placement_index += 1
                        if ship_placement_index < len(SHIPS_TO_PLACE):
                            current_ship_length = SHIPS_TO_PLACE[ship_placement_index]
                            current_ship_orientation = "horizontal" 
                        else:
                            done_placing_ships = True
                            DISPLAY_MESSAGE = "All ships placed. Waiting for opponent."
                            MESSAGE_DISPLAY_TIME = time.time() + 2.0 
                            send_data({"type": "SHIPS_PLACED"})
                            break 
                    else:
                        DISPLAY_MESSAGE = "Invalid placement (Overlap or Out of Bounds)."
                        MESSAGE_DISPLAY_TIME = time.time() + 1.5
                shooting_cursor_pos = (x, y)
    elif done_placing_ships and not opponent_ready:
        for event in pygame.event.get():
            if event.type == pygame.QUIT: pygame.quit(); sys.exit()
        data = receive_data()
        if data and data.get("type") == "SHIPS_PLACED":
            opponent_ready = True
            DISPLAY_MESSAGE = "Opponent is ready!"
            MESSAGE_DISPLAY_TIME = time.time() + 2.0

def deciding_first_turn_state():
    global first_turn_started, first_turn_decided, has_first_turn, DISPLAY_MESSAGE, MESSAGE_DISPLAY_TIME
    global waiting_for_opponent_ready
    
    for event in pygame.event.get():
        if event.type == pygame.QUIT: pygame.quit(); sys.exit()
    
    # 1. Send Ready Signal
    if not first_turn_started:
        first_turn_started = True
        waiting_for_opponent_ready = True
        send_data({"type": "READY_TO_START"})
        return
    
    # 2. Wait for Opponent's Ready Signal
    if waiting_for_opponent_ready:
        data = receive_data()
        if data and data.get("type") == "READY_TO_START":
            waiting_for_opponent_ready = False
            
            # --- SIMPLIFIED LOGIC: MASTER ALWAYS GOES FIRST ---
            if IS_MASTER_PI:
                has_first_turn = True
                DISPLAY_MESSAGE = "You are Master. You go first!"
            else:
                has_first_turn = False
                DISPLAY_MESSAGE = "Opponent is Master. They go first."
            
            MESSAGE_DISPLAY_TIME = time.time() + 2.0
            first_turn_decided = True

def shooting_state():
    global shot_fired, shooting_result_received, shooting_cursor_pos, shots_fired
    global DISPLAY_MESSAGE, MESSAGE_DISPLAY_TIME, game_over, last_sent_shot, result_display_time
    
    if not shot_fired:
        for event in pygame.event.get():
            if event.type == pygame.QUIT: pygame.quit(); sys.exit()
            if event.type == pygame.KEYDOWN:
                x, y = shooting_cursor_pos
                if event.key == pygame.K_RIGHT: x = (x + 1) % GRID_SIZE
                elif event.key == pygame.K_DOWN: y = (y + 1) % GRID_SIZE
                elif event.key == pygame.K_LEFT: x = (x - 1 + GRID_SIZE) % GRID_SIZE 
                elif event.key == pygame.K_UP: y = (y - 1 + GRID_SIZE) % GRID_SIZE 
                elif event.key == pygame.K_SPACE: 
                    target_pos = (x, y)
                    if target_pos in shots_fired:
                        DISPLAY_MESSAGE = "Already shot here!"
                        MESSAGE_DISPLAY_TIME = time.time() + 1.5
                        continue
                    shot_data = {"type": "SHOT", "coord": target_pos}
                    if send_data(shot_data):
                        shots_fired[target_pos] = None 
                        last_sent_shot = target_pos
                        shot_fired = True
                        DISPLAY_MESSAGE = f"Firing shot at {target_pos}..."
                        MESSAGE_DISPLAY_TIME = time.time() + 1.0
                        break
                shooting_cursor_pos = (x, y)
    elif shot_fired and not shooting_result_received:
        for event in pygame.event.get():
            if event.type == pygame.QUIT: pygame.quit(); sys.exit()
        data = receive_data()
        if data and data.get("type") == "SHOT_RESULT":
            shooting_result = data.get("result")
            coord = tuple(data.get("coord"))
            if coord == last_sent_shot:
                shots_fired[coord] = shooting_result 
                DISPLAY_MESSAGE = f"Result: {shooting_result} at {coord}"
                MESSAGE_DISPLAY_TIME = time.time() + 3.0 
                result_display_time = time.time() + 3.0 
                if shooting_result == "ALL_SUNK": game_over = True
                shooting_result_received = True

def receiving_state():
    global shooting_result_sent, game_over, my_board_shots, processed_shot_coords
    global DISPLAY_MESSAGE, MESSAGE_DISPLAY_TIME, result_display_time
    
    for event in pygame.event.get():
        if event.type == pygame.QUIT: pygame.quit(); sys.exit()
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
        DISPLAY_MESSAGE = f"Enemy shot at {enemy_shot_coord}. Result: {result}."
        MESSAGE_DISPLAY_TIME = time.time() + 3.0 
        
        response_data = {"type": "SHOT_RESULT", "coord": enemy_shot_coord, "result": result}
        if send_data(response_data):
            shooting_result_sent = True
            result_display_time = time.time() + 3.0
        else: print("ERROR: Failed to send shot result.")

def end_state():
    for event in pygame.event.get():
        if event.type == pygame.QUIT: pygame.quit(); sys.exit()

def next_state():
    global game_state, is_connected, done_placing_ships, first_turn_decided, has_first_turn
    global shooting_result_received, shooting_result_sent, game_over, shot_fired, opponent_ready
    global waiting_for_opponent_ready, result_display_time
    
    # Don't transition if message is still displaying (except for critical states)
    if DISPLAY_MESSAGE and time.time() < MESSAGE_DISPLAY_TIME:
        # Added DECIDING_FIRST_TURN so the text "You are Master" doesn't block the transition
        if game_state not in ["RECEIVING", "SHOOTING", "DECIDING_FIRST_TURN"]: return

    if game_state == "WAITING":
        if is_connected: game_state = "PLACING_SHIPS"; is_connected = False
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

if __name__ == "__main__":
    try:
        while True:             
            next_state()
            perform_state()
            update_screen()
            CLOCK.tick(60)
    except KeyboardInterrupt:
        pygame.quit(); sys.exit()
    except SystemExit:
        pygame.quit()