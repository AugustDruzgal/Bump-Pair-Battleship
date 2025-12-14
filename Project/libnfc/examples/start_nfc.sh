#
#   In the directory /home/pi/Project/libnfc/examples
#
#   nfc-dep-initiator.c compiles with the command "make nfc-dep-initiator" to the executable nfc-dep-initiator
#  
#   This program alternates between DEP (Data Exchange Protocol) initiator mode and DEP target mode, with a random
#   duration spent in target mode to ensure that both devices act in both modes. In initiator mode, the device sends
#   its bluetooth address to the other device if it encounters a device in target mode. When a bluetooth address is
#   sent from one device to the other, the receiving (target mode) device sends it to the BT_ADDR_PIPE pipe, which 
#   is then used by the Bluetooth program to connect to the advertising device as a client. 
#

# Set the libnfc configuration to use the UART-connected pn532 device
export LIBNFC_DEVICE=pn532_uart:/dev/serial0

# Set the bluetooth address as an environment variable, so the NFC program can use it
export BT_ADDR=$(bluetoothctl show | grep "Controller" | awk '{print $2}')

# Set the pipe for sending the bluetooth address from the NFC 
export BT_ADDR_PIPE=/home/pi/Project/bt_addr_pipe

cd /home/pi/Project/libnfc/examples
make nfc-dep-initiator

# Run the NFC executable
./nfc-dep-initiator &

cd /home/pi/Project

python3 bluetooth_peripheral.py