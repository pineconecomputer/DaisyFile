import socket

HOST = "10.0.0.225"
PORT = 6502

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
	s.bind((HOST, PORT))
	s.listen()
	conn, addr = s.accept()
	with conn:
		print(f"Connection from {addr}")
		conn.sendall(b"Echo server connection established.\n\rHello {addr}.")
		while True:
			data = conn.recv(1024)
			if not data:
				break;
			conn.sendall(data)
