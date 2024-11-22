import sys
import os
import paramiko
import stat  # Import the stat module for S_ISDIR
import threading
from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QLineEdit, QPushButton,
    QFileDialog, QTextEdit, QVBoxLayout, QHBoxLayout, QGroupBox,
    QMessageBox, QGridLayout, QRadioButton, QButtonGroup,
    QProgressBar, QTreeView, QSplitter, QTabWidget,
    QAbstractItemView, QMenu, QInputDialog
)
from PyQt6.QtCore import Qt, pyqtSignal, QObject, QThread, QModelIndex, QDir
from PyQt6.QtGui import QTextCursor, QColor, QStandardItemModel, QStandardItem, QFileSystemModel, QAction
import webbrowser


class LogEmitter(QObject):
    log_signal = pyqtSignal(str, str)  # message, color


class FileTransferWorker(QThread):
    # Define signals for success, error, and progress
    transfer_finished = pyqtSignal(str)
    transfer_error = pyqtSignal(str)
    progress_update = pyqtSignal(int)

    def __init__(self, params, log_emitter):
        super().__init__()
        self.ip = params['ip']
        self.port = params['port']
        self.username = params['username']
        self.password = params['password']
        self.destination = params['destination']
        self.selected_files = params['selected_files']
        self.selection_mode = params['selection_mode']
        self.exclusions = params['exclusions']
        self.log_emitter = log_emitter
        self.stop_event = threading.Event()
        self.total_size = 0
        self.transferred_size = 0
        self.common_path = ""  # Initialize as instance variable

    def run(self):
        self.log("Starting file transfer...", "blue")
        try:
            # Establish SSH connection
            self.log("Establishing SSH connection...", "cyan")
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(
                self.ip,
                port=int(self.port),
                username=self.username,
                password=self.password,
                timeout=10
            )
            self.log("SSH connection established.", "green")

            # Fetch and display system information
            self.fetch_system_info(ssh)

            sftp = ssh.open_sftp()

            # Check if destination directory exists, if not, create it
            try:
                sftp.chdir(self.destination)
                self.log(f"Destination directory exists: {self.destination}", "green")
            except IOError:
                self.log(f"Destination directory '{self.destination}' does not exist. Creating it...", "yellow")
                self.makedirs(sftp, self.destination)
                self.log(f"Created directory: {self.destination}", "green")

            # Calculate total size
            self.total_size = self.calculate_total_size()
            self.transferred_size = 0
            self.log(f"Total size to upload: {self.total_size / (1024 * 1024):.2f} MB", "blue")

            # Start uploading
            self.common_path = os.path.commonpath(self.selected_files)  # Define as instance variable
            if os.path.isfile(self.common_path):
                # If the common path is a file, set the common directory
                self.common_path = os.path.dirname(self.common_path)

            for local_file in self.selected_files:
                if self.stop_event.is_set():
                    self.log("Transfer terminated by the user.", "yellow")
                    self.transfer_finished.emit("terminated")
                    return

                # Determine if the path is a file or directory
                if os.path.isfile(local_file):
                    relative_path = os.path.relpath(local_file, self.common_path)  # Use instance variable
                    remote_file = os.path.join(self.destination, relative_path).replace('\\', '/')
                    self.upload_file(sftp, local_file, remote_file)
                elif os.path.isdir(local_file):
                    relative_dir = os.path.relpath(local_file, self.common_path)  # Use instance variable
                    remote_dir = os.path.join(self.destination, relative_dir).replace('\\', '/')
                    self.upload_directory(sftp, local_file, remote_dir)

            sftp.close()
            ssh.close()
            if not self.stop_event.is_set():
                self.log("File transfer completed successfully.", "green")
                self.transfer_finished.emit("success")
        except Exception as e:
            self.log(f"Error: {str(e)}", "red")
            self.transfer_error.emit(f"An error occurred: {str(e)}")
        finally:
            self.log("Transfer thread finished.", "grey")

    def upload_file(self, sftp, local_path, remote_path):
        base_name = os.path.basename(local_path)
        if base_name in self.exclusions:
            self.log(f"Excluded file: {base_name}", "yellow")
            return

        remote_dir = os.path.dirname(remote_path)
        try:
            sftp.chdir(remote_dir)
        except IOError:
            self.makedirs(sftp, remote_dir)
            self.log(f"Created directory: {remote_dir}", "green")

        self.log(f"Uploading {local_path} to {remote_path}", "blue")
        try:
            sftp.put(local_path, remote_path, callback=self.create_callback())
            if self.stop_event.is_set():
                self.log("Transfer terminated during file upload.", "yellow")
                self.transfer_finished.emit("terminated")
                return
            self.log(f"Uploaded {base_name}", "green")
        except Exception as e:
            self.log(f"Failed to upload {base_name}: {str(e)}", "red")
            self.transfer_error.emit(f"Failed to upload {base_name}: {str(e)}")

    def upload_directory(self, sftp, local_dir, remote_dir):
        base_name = os.path.basename(local_dir)
        if base_name in self.exclusions:
            self.log(f"Excluded directory: {base_name}", "yellow")
            return

        try:
            sftp.chdir(remote_dir)
        except IOError:
            self.makedirs(sftp, remote_dir)
            self.log(f"Created directory: {remote_dir}", "green")

        for root, dirs, files in os.walk(local_dir):
            # Apply exclusions to directories
            dirs[:] = [d for d in dirs if d not in self.exclusions]

            relative_root = os.path.relpath(root, self.common_path)  # Use instance variable
            current_remote_dir = os.path.join(self.destination, relative_root).replace('\\', '/')
            try:
                sftp.chdir(current_remote_dir)
            except IOError:
                self.makedirs(sftp, current_remote_dir)
                self.log(f"Created directory: {current_remote_dir}", "green")

            for file in files:
                if file in self.exclusions:
                    self.log(f"Excluded file: {file}", "yellow")
                    continue
                local_file = os.path.join(root, file)
                remote_file = os.path.join(current_remote_dir, file).replace('\\', '/')
                self.upload_file(sftp, local_file, remote_file)

    def calculate_total_size(self):
        total = 0
        for local_file in self.selected_files:
            if os.path.isfile(local_file):
                try:
                    size = os.path.getsize(local_file)
                    total += size
                except Exception as e:
                    self.log(f"Error getting size for {local_file}: {str(e)}", "red")
            elif os.path.isdir(local_file):
                for root, dirs, files in os.walk(local_file):
                    # Exclude directories
                    dirs[:] = [d for d in dirs if d not in self.exclusions]
                    for file in files:
                        if file in self.exclusions:
                            continue
                        try:
                            size = os.path.getsize(os.path.join(root, file))
                            total += size
                        except Exception as e:
                            self.log(f"Error getting size for {os.path.join(root, file)}: {str(e)}", "red")
        return total

    def create_callback(self):
        def callback(transferred, total):
            if self.stop_event.is_set():
                return
            self.transferred_size += transferred
            if self.total_size > 0:
                percentage = int((self.transferred_size / self.total_size) * 100)
                self.progress_update.emit(percentage)
        return callback

    def stop(self):
        self.stop_event.set()

    def log(self, message, color="white"):
        color_dict = {
            "white": "#FFFFFF",
            "green": "#00FF00",
            "red": "#FF0000",
            "yellow": "#FFFF00",
            "blue": "#0000FF",
            "cyan": "#00FFFF",
            "magenta": "#FF00FF",
            "grey": "#808080"
        }
        color_code = color_dict.get(color.lower(), "#FFFFFF")
        self.log_emitter.log_signal.emit(message, color_code)

    def fetch_system_info(self, ssh):
        commands = {
            "Uptime": "uptime -p",
            "Disk Usage": "df -h /",
            "Memory Usage": "free -h",
            "System Info": "uname -a"
        }
        self.log("Fetching VPS system information...", "cyan")
        for key, cmd in commands.items():
            if self.stop_event.is_set():
                self.log("Transfer terminated. Stopping system info fetch.", "yellow")
                return
            stdin, stdout, stderr = ssh.exec_command(cmd)
            output = stdout.read().decode().strip()
            error = stderr.read().decode().strip()
            if error:
                self.log(f"{key}: Error - {error}", "red")
            else:
                self.log(f"{key}: {output}", "blue")
        self.log("System information fetched successfully.", "green")

    def makedirs(self, sftp, remote_directory):
        dirs = remote_directory.strip('/').split('/')
        path = ""
        for dir in dirs:
            path += f"/{dir}"
            try:
                sftp.chdir(path)
            except IOError:
                try:
                    sftp.mkdir(path)
                    self.log(f"Created directory: {path}", "green")
                except Exception as e:
                    self.log(f"Failed to create directory {path}: {str(e)}", "red")


class RemoteFileOperationWorker(QThread):
    # Define signals for operation completion and errors
    operation_finished = pyqtSignal(str, str)  # message, color

    def __init__(self, operation, params):
        super().__init__()
        self.operation = operation  # 'download', 'delete', 'rename', 'create_dir', 'move'
        self.params = params  # Dictionary containing necessary parameters

    def run(self):
        try:
            ip = self.params['ip']
            port = self.params['port']
            username = self.params['username']
            password = self.params['password']
            remote_path = self.params['remote_path']
            local_destination = self.params.get('local_destination', '')
            new_name = self.params.get('new_name', '')
            move_destination = self.params.get('move_destination', '')

            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(ip, port=int(port), username=username, password=password, timeout=10)
            sftp = ssh.open_sftp()

            if self.operation == 'download':
                self.download(sftp, remote_path, local_destination)
            elif self.operation == 'delete':
                self.delete(sftp, remote_path)
            elif self.operation == 'rename':
                self.rename(sftp, remote_path, new_name)
            elif self.operation == 'create_dir':
                self.create_directory(sftp, remote_path)
            elif self.operation == 'move':
                self.move(sftp, remote_path, move_destination)

            sftp.close()
            ssh.close()
            if self.operation in ['download', 'delete', 'rename', 'create_dir', 'move']:
                self.operation_finished.emit(f"{self.operation.capitalize()} operation completed successfully.", "green")
        except Exception as e:
            self.operation_finished.emit(f"Error during {self.operation}: {str(e)}", "red")

    def download(self, sftp, remote_path, local_destination):
        try:
            if self.is_dir(sftp, remote_path):
                self.recursive_download(sftp, remote_path, local_destination)
            else:
                filename = os.path.basename(remote_path)
                local_path = os.path.join(local_destination, filename)
                sftp.get(remote_path, local_path)
                self.operation_finished.emit(f"Downloaded: {remote_path}", "green")
        except Exception as e:
            self.operation_finished.emit(f"Failed to download {remote_path}: {str(e)}", "red")

    def recursive_download(self, sftp, remote_dir, local_dir):
        try:
            if not os.path.exists(local_dir):
                os.makedirs(local_dir)
            for item in sftp.listdir_attr(remote_dir):
                remote_path = os.path.join(remote_dir, item.filename).replace('\\', '/')
                local_path = os.path.join(local_dir, item.filename)
                if stat.S_ISDIR(item.st_mode):
                    self.recursive_download(sftp, remote_path, local_path)
                else:
                    sftp.get(remote_path, local_path)
                    self.operation_finished.emit(f"Downloaded: {remote_path}", "green")
        except Exception as e:
            self.operation_finished.emit(f"Failed to download directory {remote_dir}: {str(e)}", "red")

    def delete(self, sftp, remote_path):
        try:
            if self.is_dir(sftp, remote_path):
                self.recursive_delete(sftp, remote_path)
            else:
                sftp.remove(remote_path)
                self.operation_finished.emit(f"Deleted: {remote_path}", "green")
        except Exception as e:
            self.operation_finished.emit(f"Failed to delete {remote_path}: {str(e)}", "red")

    def rename(self, sftp, remote_path, new_name):
        try:
            base_dir = os.path.dirname(remote_path)
            new_remote_path = os.path.join(base_dir, new_name).replace('\\', '/')
            sftp.rename(remote_path, new_remote_path)
            self.operation_finished.emit(f"Renamed to: {new_remote_path}", "green")
        except Exception as e:
            self.operation_finished.emit(f"Failed to rename {remote_path}: {str(e)}", "red")

    def create_directory(self, sftp, remote_path):
        try:
            sftp.mkdir(remote_path)
            self.operation_finished.emit(f"Created directory: {remote_path}", "green")
        except Exception as e:
            self.operation_finished.emit(f"Failed to create directory {remote_path}: {str(e)}", "red")

    def move(self, sftp, remote_path, move_destination):
        try:
            base_dir = os.path.dirname(remote_path)
            item_name = os.path.basename(remote_path)
            new_remote_path = os.path.join(move_destination, item_name).replace('\\', '/')
            sftp.rename(remote_path, new_remote_path)
            self.operation_finished.emit(f"Moved to: {new_remote_path}", "green")
        except Exception as e:
            self.operation_finished.emit(f"Failed to move {remote_path}: {str(e)}", "red")

    def is_dir(self, sftp, path):
        try:
            return stat.S_ISDIR(sftp.stat(path).st_mode)
        except IOError:
            return False

    def recursive_delete(self, sftp, remote_dir):
        try:
            for item in sftp.listdir_attr(remote_dir):
                remote_path = os.path.join(remote_dir, item.filename).replace('\\', '/')
                if stat.S_ISDIR(item.st_mode):
                    self.recursive_delete(sftp, remote_path)
                else:
                    sftp.remove(remote_path)
                    self.operation_finished.emit(f"Deleted: {remote_path}", "green")
            sftp.rmdir(remote_dir)
            self.operation_finished.emit(f"Deleted directory: {remote_dir}", "green")
        except Exception as e:
            self.operation_finished.emit(f"Failed to delete directory {remote_dir}: {str(e)}", "red")

    def recursive_download(self, sftp, remote_dir, local_dir):
        try:
            if not os.path.exists(local_dir):
                os.makedirs(local_dir)
            for item in sftp.listdir_attr(remote_dir):
                remote_path = os.path.join(remote_dir, item.filename).replace('\\', '/')
                local_path = os.path.join(local_dir, item.filename)
                if stat.S_ISDIR(item.st_mode):
                    self.recursive_download(sftp, remote_path, local_path)
                else:
                    sftp.get(remote_path, local_path)
                    self.operation_finished.emit(f"Downloaded: {remote_path}", "green")
        except Exception as e:
            self.operation_finished.emit(f"Failed to download directory {remote_dir}: {str(e)}", "red")


class FileTransferApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("VPS File Transfer Tool")
        self.setGeometry(100, 100, 1600, 900)  # Increased window size for better layout
        self.setStyleSheet("""
            QWidget {
                background-color: #2e2e2e;
                color: #d4d4d4;
                font-family: Arial, sans-serif;
            }
            QGroupBox {
                border: 1px solid #5c5c5c;
                border-radius: 5px;
                margin-top: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px 0 5px;
            }
            QTreeView::item:hover {
                background-color: #555555;
            }
            QHeaderView::section {
                background-color: #2e2e2e;
                color: #ffffff;
                padding: 4px;
                border: 1px solid #5c5c5c;
            }
        """)

        # Initialize Log Emitter
        self.log_emitter = LogEmitter()
        self.log_emitter.log_signal.connect(self.append_log)

        # Initialize workers
        self.transfer_worker = None
        self.remote_worker = None

        # Initialize selected files list
        self.selected_files = []

        # Initialize remote base path
        self.remote_base_path = ""

        # Initialize processing state
        self.processing = False

        self.init_ui()

    def init_ui(self):
        main_layout = QHBoxLayout(self)

        # Splitter to divide main area and tabs
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Main functionality area with Tabs
        main_function_widget = QWidget()
        main_function_layout = QVBoxLayout(main_function_widget)

        # Tabs for Local, Remote Explorer, and Developer Info
        self.tabs = QTabWidget()
        self.tabs.setStyleSheet("QTabBar::tab { height: 30px; width: 180px; }")
        self.local_tab = QWidget()
        self.remote_tab = QWidget()
        self.developer_tab = QWidget()  # Developer Info Tab
        self.tabs.addTab(self.local_tab, "Local Explorer")
        self.tabs.addTab(self.remote_tab, "Remote Explorer")
        self.tabs.addTab(self.developer_tab, "Developer Info")

        # Initialize Local Explorer
        self.init_local_tab()

        # Initialize Remote Explorer
        self.init_remote_tab()

        # Initialize Developer Info Tab
        self.init_developer_tab()

        main_function_layout.addWidget(self.tabs)

        # Credentials and Settings
        settings_group = QGroupBox("Settings")
        settings_layout = QGridLayout()

        # VPS IP Address
        ip_label = QLabel("VPS IP Address:")
        ip_label.setStyleSheet("color: #ffffff;")
        self.ip_input = QLineEdit()
        self.ip_input.setPlaceholderText("203.161.49.225")
        self.ip_input.setStyleSheet("background-color: #1e1e1e; color: #d4d4d4;")
        self.ip_input.setFixedHeight(30)

        # Port
        port_label = QLabel("Port:")
        port_label.setStyleSheet("color: #ffffff;")
        self.port_input = QLineEdit()
        self.port_input.setPlaceholderText("22")
        self.port_input.setStyleSheet("background-color: #1e1e1e; color: #d4d4d4;")
        self.port_input.setFixedHeight(30)

        # Username
        username_label = QLabel("Username:")
        username_label.setStyleSheet("color: #ffffff;")
        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("root")
        self.username_input.setStyleSheet("background-color: #1e1e1e; color: #d4d4d4;")
        self.username_input.setFixedHeight(30)

        # Password
        password_label = QLabel("Password:")
        password_label.setStyleSheet("color: #ffffff;")
        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("Password")
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setStyleSheet("background-color: #1e1e1e; color: #d4d4d4;")
        self.password_input.setFixedHeight(30)

        # Destination Directory
        dest_label = QLabel("Destination Directory on VPS:")
        dest_label.setStyleSheet("color: #ffffff;")
        self.dest_path = QLineEdit()
        self.dest_path.setPlaceholderText("/home/user/destination/")
        self.dest_path.setStyleSheet("background-color: #1e1e1e; color: #d4d4d4;")
        self.dest_path.setFixedHeight(30)

        # Exclusions
        exclusions_label = QLabel("Exclusions (comma-separated):")
        exclusions_label.setStyleSheet("color: #ffffff;")
        self.exclusions_input = QLineEdit()
        self.exclusions_input.setPlaceholderText("e.g., migrations, venv, __pycache__")
        self.exclusions_input.setStyleSheet("background-color: #1e1e1e; color: #d4d4d4;")
        self.exclusions_input.setFixedHeight(30)

        # Adding widgets to settings layout
        settings_layout.addWidget(ip_label, 0, 0)
        settings_layout.addWidget(self.ip_input, 0, 1)
        settings_layout.addWidget(port_label, 0, 2)
        settings_layout.addWidget(self.port_input, 0, 3)
        settings_layout.addWidget(username_label, 1, 0)
        settings_layout.addWidget(self.username_input, 1, 1)
        settings_layout.addWidget(password_label, 1, 2)
        settings_layout.addWidget(self.password_input, 1, 3)
        settings_layout.addWidget(dest_label, 2, 0)
        settings_layout.addWidget(self.dest_path, 2, 1, 1, 3)
        settings_layout.addWidget(exclusions_label, 3, 0)
        settings_layout.addWidget(self.exclusions_input, 3, 1, 1, 3)

        settings_group.setLayout(settings_layout)
        main_function_layout.addWidget(settings_group)

        # Progress Bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: 2px solid grey;
                border-radius: 5px;
                text-align: center;
            }
            QProgressBar::chunk {
                background-color: #05B8CC;
                width: 20px;
            }
        """)
        self.progress_bar.setValue(0)
        main_function_layout.addWidget(self.progress_bar)

        # Buttons Layout
        buttons_layout = QHBoxLayout()

        # Transfer Button
        self.transfer_btn = QPushButton("Transfer Files")
        self.transfer_btn.clicked.connect(self.start_transfer)
        self.transfer_btn.setStyleSheet("""
            QPushButton {
                background-color: #4caf50;
                color: white;
                border: none;
                padding: 10px 20px;
                font-size: 14px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
            QPushButton:disabled {
                background-color: #9e9e9e;
            }
        """)
        self.transfer_btn.setFixedHeight(40)

        # Terminate Button
        self.terminate_btn = QPushButton("Terminate Transfer")
        self.terminate_btn.clicked.connect(self.terminate_transfer)
        self.terminate_btn.setStyleSheet("""
            QPushButton {
                background-color: #f44336;
                color: white;
                border: none;
                padding: 10px 20px;
                font-size: 14px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #da190b;
            }
            QPushButton:disabled {
                background-color: #9e9e9e;
            }
        """)
        self.terminate_btn.setEnabled(False)
        self.terminate_btn.setFixedHeight(40)

        buttons_layout.addWidget(self.transfer_btn)
        buttons_layout.addWidget(self.terminate_btn)
        main_function_layout.addLayout(buttons_layout)

        # Log Section
        log_group = QGroupBox("Transfer Log")
        log_layout = QVBoxLayout()

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setStyleSheet("""
            QTextEdit {
                background-color: #1e1e1e;
                color: #d4d4d4;
                font-family: Consolas;
                font-size: 12px;
            }
        """)
        log_layout.addWidget(self.log_text)
        log_group.setLayout(log_layout)
        main_function_layout.addWidget(log_group)

        # Add stretch to make logs take up available space
        main_function_layout.addStretch()

        splitter.addWidget(main_function_widget)

        main_layout.addWidget(splitter)

    def init_local_tab(self):
        layout = QVBoxLayout()

        # File System Model
        self.local_model = QFileSystemModel()
        self.local_model.setRootPath(QDir.homePath())

        # Tree View
        self.local_tree = QTreeView()
        self.local_tree.setModel(self.local_model)
        self.local_tree.setRootIndex(self.local_model.index(QDir.homePath()))
        self.local_tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.local_tree.setAnimated(True)
        self.local_tree.setIndentation(20)
        self.local_tree.setSortingEnabled(True)
        self.local_tree.setStyleSheet("""
            QTreeView {
                background-color: #1e1e1e;
                color: #d4d4d4;
                selection-background-color: #555555;
                selection-color: #ffffff;
            }
        """)
        self.local_tree.setColumnWidth(0, 300)
        self.local_tree.setAlternatingRowColors(True)
        self.local_tree.setUniformRowHeights(True)
        layout.addWidget(self.local_tree)

        self.local_tab.setLayout(layout)

    def init_remote_tab(self):
        layout = QVBoxLayout()

        # Remote File System Model
        self.remote_model = QStandardItemModel()
        self.remote_model.setHorizontalHeaderLabels(['Name', 'Size', 'Type'])

        # Tree View
        self.remote_tree = QTreeView()
        self.remote_tree.setModel(self.remote_model)
        self.remote_tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.remote_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.remote_tree.customContextMenuRequested.connect(self.remote_context_menu)
        self.remote_tree.setStyleSheet("""
            QTreeView {
                background-color: #1e1e1e;
                color: #d4d4d4;
                selection-background-color: #555555;
                selection-color: #ffffff;
            }
            QHeaderView::section {
                background-color: #2e2e2e;
                color: #ffffff;
                padding: 4px;
                border: 1px solid #5c5c5c;
            }
            QTreeView::item:hover {
                background-color: #555555;
            }
        """)
        self.remote_tree.setColumnWidth(0, 250)
        self.remote_tree.setColumnWidth(1, 100)
        self.remote_tree.setColumnWidth(2, 100)
        self.remote_tree.setAlternatingRowColors(True)
        self.remote_tree.setUniformRowHeights(True)
        layout.addWidget(self.remote_tree)

        # Connect the expanded and clicked signals for lazy loading and updating destination
        self.remote_tree.expanded.connect(self.on_remote_tree_expanded)
        self.remote_tree.clicked.connect(self.on_remote_tree_clicked)  # Connect click to update destination

        # Load Remote Files Button
        load_remote_btn = QPushButton("Load Remote Directory")
        load_remote_btn.clicked.connect(self.load_remote_directory)
        load_remote_btn.setStyleSheet("""
            QPushButton {
                background-color: #3c3c3c;
                color: #d4d4d4;
                border: 1px solid #5c5c5c;
                padding: 5px 10px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #5c5c5c;
            }
        """)
        load_remote_btn.setFixedHeight(30)
        layout.addWidget(load_remote_btn)

        self.remote_tab.setLayout(layout)

    def init_developer_tab(self):
        layout = QVBoxLayout()

        dev_label = QLabel("<b>thehackitect</b>")
        dev_label.setStyleSheet("color: #ffffff; font-size: 14px;")
        dev_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(dev_label)

        # Social Buttons
        social_layout = QHBoxLayout()

        # GitHub Button
        github_btn = QPushButton("GitHub")
        github_btn.clicked.connect(lambda: self.open_url("https://github.com/thehackitect"))
        github_btn.setStyleSheet("""
            QPushButton {
                background-color: #24292e;
                color: white;
                border: none;
                padding: 6px 12px;
                font-size: 12px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #444d56;
            }
        """)

        # Twitter Button
        twitter_btn = QPushButton("Twitter")
        twitter_btn.clicked.connect(lambda: self.open_url("https://twitter.com/thehackitect"))
        twitter_btn.setStyleSheet("""
            QPushButton {
                background-color: #1da1f2;
                color: white;
                border: none;
                padding: 6px 12px;
                font-size: 12px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #0d95e8;
            }
        """)

        # LinkedIn Button
        linkedin_btn = QPushButton("LinkedIn")
        linkedin_btn.clicked.connect(lambda: self.open_url("https://linkedin.com/in/thehackitect"))
        linkedin_btn.setStyleSheet("""
            QPushButton {
                background-color: #0077B5;
                color: white;
                border: none;
                padding: 6px 12px;
                font-size: 12px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #005582;
            }
        """)

        social_layout.addWidget(github_btn)
        social_layout.addWidget(twitter_btn)
        social_layout.addWidget(linkedin_btn)
        layout.addLayout(social_layout)

        # Additional Developer Info (Email, Website)
        social_layout_dev = QHBoxLayout()

        email_btn = QPushButton("Email")
        email_btn.clicked.connect(lambda: self.open_url("mailto:thehackitect.bots@gmail.com"))
        email_btn.setStyleSheet("""
            QPushButton {
                background-color: #6c757d;
                color: white;
                border: none;
                padding: 6px 12px;
                font-size: 12px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #5a6268;
            }
        """)

        website_btn = QPushButton("Website")
        website_btn.clicked.connect(lambda: self.open_url("https://www.thehackitect.com"))
        website_btn.setStyleSheet("""
            QPushButton {
                background-color: #343a40;
                color: white;
                border: none;
                padding: 6px 12px;
                font-size: 12px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #23272b;
            }
        """)

        social_layout_dev.addWidget(email_btn)
        social_layout_dev.addWidget(website_btn)
        layout.addLayout(social_layout_dev)

        self.developer_tab.setLayout(layout)

    def remote_context_menu(self, position):
        if self.processing:
            # If processing is ongoing, do not show the context menu
            return

        indexes = self.remote_tree.selectedIndexes()
        if indexes:
            menu = QMenu()

            download_action = QAction("Download", self)
            download_action.triggered.connect(self.download_remote_files)
            menu.addAction(download_action)

            delete_action = QAction("Delete", self)
            delete_action.triggered.connect(self.delete_remote_files)
            menu.addAction(delete_action)

            rename_action = QAction("Rename", self)
            rename_action.triggered.connect(self.rename_remote_file)
            menu.addAction(rename_action)

            create_dir_action = QAction("Create Directory", self)
            create_dir_action.triggered.connect(self.create_remote_directory)
            menu.addAction(create_dir_action)

            move_action = QAction("Move", self)
            move_action.triggered.connect(self.move_remote_file)
            menu.addAction(move_action)

            menu.exec(self.remote_tree.viewport().mapToGlobal(position))

    def load_remote_directory(self):
        destination = self.dest_path.text().strip()
        if not destination:
            QMessageBox.warning(self, "Warning", "Please enter the destination directory on the VPS.")
            return

        # Clear existing items
        self.remote_model.removeRows(0, self.remote_model.rowCount())

        # Establish SSH connection and list remote directory
        try:
            ip = self.ip_input.text().strip()
            port = self.port_input.text().strip()
            username = self.username_input.text().strip()
            password = self.password_input.text().strip()

            if not all([ip, port, username, password, destination]):
                QMessageBox.warning(self, "Warning", "Please fill in all required fields.")
                return

            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(ip, port=int(port), username=username, password=password, timeout=10)
            sftp = ssh.open_sftp()

            self.remote_model.setHorizontalHeaderLabels(['Name', 'Size', 'Type'])

            self.populate_remote_tree(sftp, destination, self.remote_model.invisibleRootItem())

            sftp.close()
            ssh.close()

            # Store the base path
            self.remote_base_path = destination.rstrip('/')

            self.log(f"Loaded remote directory: {destination}", "green")
        except Exception as e:
            self.log(f"Error loading remote directory: {str(e)}", "red")
            QMessageBox.critical(self, "Error", f"Failed to load remote directory: {str(e)}")

    def populate_remote_tree(self, sftp, path, parent_item):
        try:
            for entry in sftp.listdir_attr(path):
                item = QStandardItem(entry.filename)
                size_item = QStandardItem(str(entry.st_size))
                type_item = QStandardItem("Directory" if stat.S_ISDIR(entry.st_mode) else "File")
                parent_item.appendRow([item, size_item, type_item])

                if stat.S_ISDIR(entry.st_mode):
                    # Add a dummy child to make the item expandable
                    dummy = QStandardItem("Loading...")
                    item.appendRow(dummy)
        except Exception as e:
            self.log(f"Error populating remote tree: {str(e)}", "red")

    def on_remote_tree_expanded(self, index):
        if self.processing:
            # Prevent expanding while processing
            return

        item = self.remote_model.itemFromIndex(index)
        if item.hasChildren() and item.child(0).text() == "Loading...":
            item.removeRows(0, item.rowCount())
            remote_path = self.get_remote_file_path(index)
            try:
                ip = self.ip_input.text().strip()
                port = self.port_input.text().strip()
                username = self.username_input.text().strip()
                password = self.password_input.text().strip()

                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.connect(ip, port=int(port), username=username, password=password, timeout=10)
                sftp = ssh.open_sftp()

                self.populate_remote_tree(sftp, remote_path, item)

                sftp.close()
                ssh.close()
            except Exception as e:
                self.log(f"Error expanding remote tree: {str(e)}", "red")

    def on_remote_tree_clicked(self, index):
        if self.processing:
            # Prevent updating destination while processing
            return

        remote_path = self.get_remote_file_path(index)
        self.dest_path.setText(remote_path)
        self.log(f"Destination directory updated to: {remote_path}", "blue")

    def download_remote_files(self):
        selected_indexes = self.remote_tree.selectedIndexes()
        if not selected_indexes:
            QMessageBox.warning(self, "Warning", "Please select files or folders to download.")
            return

        # Extract selected file paths
        selected_files = set()
        for index in selected_indexes:
            if index.column() == 0:
                file_path = self.get_remote_file_path(index)
                selected_files.add(file_path)

        local_destination = QFileDialog.getExistingDirectory(self, "Select Download Destination")
        if not local_destination:
            return

        # Set processing state
        self.set_processing_state(True)
        self.log("Processing... Downloading selected items.", "magenta")

        # Start download in a separate thread
        for file in selected_files:
            params = {
                'ip': self.ip_input.text().strip(),
                'port': self.port_input.text().strip(),
                'username': self.username_input.text().strip(),
                'password': self.password_input.text().strip(),
                'remote_path': file,
                'local_destination': local_destination
            }
            self.remote_worker = RemoteFileOperationWorker('download', params)
            self.remote_worker.operation_finished.connect(self.handle_remote_operation_finished)
            self.remote_worker.start()

    def delete_remote_files(self):
        selected_indexes = self.remote_tree.selectedIndexes()
        if not selected_indexes:
            QMessageBox.warning(self, "Warning", "Please select files or folders to delete.")
            return

        confirm = QMessageBox.question(
            self, "Confirm Delete", "Are you sure you want to delete the selected items?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        # Extract selected file paths
        selected_files = set()
        for index in selected_indexes:
            if index.column() == 0:
                file_path = self.get_remote_file_path(index)
                selected_files.add(file_path)

        # Set processing state
        self.set_processing_state(True)
        self.log("Processing... Deleting selected items.", "magenta")

        # Start delete in a separate thread
        for file in selected_files:
            params = {
                'ip': self.ip_input.text().strip(),
                'port': self.port_input.text().strip(),
                'username': self.username_input.text().strip(),
                'password': self.password_input.text().strip(),
                'remote_path': file
            }
            self.remote_worker = RemoteFileOperationWorker('delete', params)
            self.remote_worker.operation_finished.connect(self.handle_remote_operation_finished)
            self.remote_worker.start()

    def rename_remote_file(self):
        selected_indexes = self.remote_tree.selectedIndexes()
        if not selected_indexes:
            QMessageBox.warning(self, "Warning", "Please select a file or folder to rename.")
            return

        index = selected_indexes[0]
        remote_path = self.get_remote_file_path(index)
        new_name, ok = QInputDialog.getText(self, "Rename", "Enter new name:")
        if ok and new_name:
            # Set processing state
            self.set_processing_state(True)
            self.log("Processing... Renaming item.", "magenta")

            params = {
                'ip': self.ip_input.text().strip(),
                'port': self.port_input.text().strip(),
                'username': self.username_input.text().strip(),
                'password': self.password_input.text().strip(),
                'remote_path': remote_path,
                'new_name': new_name
            }
            self.remote_worker = RemoteFileOperationWorker('rename', params)
            self.remote_worker.operation_finished.connect(self.handle_remote_operation_finished)
            self.remote_worker.start()

    def create_remote_directory(self):
        remote_parent = self.dest_path.text().strip()
        if not remote_parent:
            QMessageBox.warning(self, "Warning", "Please enter the destination directory on the VPS.")
            return

        dir_name, ok = QInputDialog.getText(self, "Create Directory", "Enter directory name:")
        if ok and dir_name:
            # Set processing state
            self.set_processing_state(True)
            self.log("Processing... Creating directory.", "magenta")

            remote_path = os.path.join(remote_parent, dir_name).replace('\\', '/')
            params = {
                'ip': self.ip_input.text().strip(),
                'port': self.port_input.text().strip(),
                'username': self.username_input.text().strip(),
                'password': self.password_input.text().strip(),
                'remote_path': remote_path
            }
            self.remote_worker = RemoteFileOperationWorker('create_dir', params)
            self.remote_worker.operation_finished.connect(self.handle_remote_operation_finished)
            self.remote_worker.start()

    def move_remote_file(self):
        selected_indexes = self.remote_tree.selectedIndexes()
        if not selected_indexes:
            QMessageBox.warning(self, "Warning", "Please select a file or folder to move.")
            return

        index = selected_indexes[0]
        remote_path = self.get_remote_file_path(index)
        move_destination, ok = QInputDialog.getText(self, "Move", "Enter destination path:")
        if ok and move_destination:
            # Set processing state
            self.set_processing_state(True)
            self.log("Processing... Moving item.", "magenta")

            params = {
                'ip': self.ip_input.text().strip(),
                'port': self.port_input.text().strip(),
                'username': self.username_input.text().strip(),
                'password': self.password_input.text().strip(),
                'remote_path': remote_path,
                'move_destination': move_destination
            }
            self.remote_worker = RemoteFileOperationWorker('move', params)
            self.remote_worker.operation_finished.connect(self.handle_remote_operation_finished)
            self.remote_worker.start()

    def handle_remote_operation_finished(self, message, color):
        self.append_log(message, color)
        # Refresh the remote directory view after operation completes
        self.refresh_remote_directory()
        # Reset processing state
        self.set_processing_state(False)

    def get_remote_file_path(self, index):
        path = []
        while index.isValid():
            path.insert(0, index.data())
            index = index.parent()
        # Combine with the remote base path
        return f"{self.remote_base_path}/" + "/".join(path)

    def start_transfer(self):
        if self.processing:
            QMessageBox.warning(self, "Warning", "Another operation is in progress. Please wait.")
            return

        selected_indexes = self.local_tree.selectedIndexes()
        if not selected_indexes:
            self.log("No files selected for transfer.", "red")
            QMessageBox.warning(self, "Warning", "Please select files or a folder to transfer.")
            return

        # Extract selected file paths
        selected_paths = set()
        for index in selected_indexes:
            if index.column() == 0:
                file_path = self.local_model.filePath(index)
                if os.path.isfile(file_path):
                    selected_paths.add(file_path)
                elif os.path.isdir(file_path):
                    selected_paths.add(file_path)  # Add directory itself

        if not selected_paths:
            self.log("No valid files selected for transfer.", "red")
            QMessageBox.warning(self, "Warning", "No valid files selected for transfer.")
            return

        self.selected_files = list(selected_paths)

        # Gather parameters
        params = {
            'ip': self.ip_input.text().strip(),
            'port': self.port_input.text().strip(),
            'username': self.username_input.text().strip(),
            'password': self.password_input.text().strip(),
            'destination': self.dest_path.text().strip(),
            'selected_files': self.selected_files,
            'selection_mode': "directories" if any(os.path.isdir(f) for f in self.selected_files) else "files",
            'exclusions': [item.strip() for item in self.exclusions_input.text().split(',') if item.strip()]
        }

        # Validate inputs
        if not all([params['ip'], params['port'], params['username'], params['password'], params['destination']]):
            self.log("Error: Please fill in all required fields.", "red")
            QMessageBox.critical(self, "Error", "Please fill in all required fields.")
            return

        # Set processing state
        self.set_processing_state(True)
        self.log("Processing... Starting file transfer.", "magenta")

        # Connect signals
        self.transfer_worker = FileTransferWorker(params, self.log_emitter)
        self.transfer_worker.transfer_finished.connect(self.on_transfer_finished)
        self.transfer_worker.transfer_error.connect(self.on_transfer_error)
        self.transfer_worker.progress_update.connect(self.update_progress)
        self.transfer_worker.start()

    def terminate_transfer(self):
        if self.transfer_worker and self.transfer_worker.isRunning():
            self.transfer_worker.stop()
            self.log("Termination signal sent. Attempting to stop the transfer...", "yellow")
            self.terminate_btn.setEnabled(False)

    def on_transfer_finished(self, status):
        if status == "success":
            QMessageBox.information(self, "Success", "Files transferred successfully.")
        elif status == "terminated":
            QMessageBox.warning(self, "Terminated", "File transfer was terminated.")
        # Reset buttons and processing state
        self.set_processing_state(False)
        self.progress_bar.setValue(0)

    def on_transfer_error(self, error_message):
        QMessageBox.critical(self, "Error", error_message)
        # Reset buttons and processing state
        self.set_processing_state(False)
        self.progress_bar.setValue(0)

    def update_progress(self, percentage):
        self.progress_bar.setValue(percentage)

    def append_log(self, message, color):
        self.log_text.setTextColor(QColor(color))
        self.log_text.append(message)
        self.log_text.moveCursor(QTextCursor.MoveOperation.End)

    def log(self, message, color="white"):
        """
        Logs a message with the specified color.
        Colors: white, green, red, yellow, blue, cyan, magenta, grey
        """
        self.log_emitter.log_signal.emit(message, color)

    def open_url(self, url):
        webbrowser.open(url)

    def set_processing_state(self, state):
        """
        Sets the processing state of the application.
        When processing, disable certain UI elements to prevent user actions.
        """
        self.processing = state
        if state:
            # Disable Remote Explorer interactions
            self.remote_tree.setEnabled(False)
            # Optionally, disable other UI elements if necessary
            self.log_text.setEnabled(False)
            self.transfer_btn.setEnabled(False)
            self.remote_tree.setCursor(Qt.CursorShape.WaitCursor)
        else:
            # Enable Remote Explorer interactions
            self.remote_tree.setEnabled(True)
            # Re-enable other UI elements
            self.log_text.setEnabled(True)
            self.transfer_btn.setEnabled(True)
            self.remote_tree.setCursor(Qt.CursorShape.ArrowCursor)

    def refresh_remote_directory(self):
        """
        Refreshes the remote directory view.
        If the current directory no longer exists, navigate to the parent directory.
        """
        try:
            ip = self.ip_input.text().strip()
            port = self.port_input.text().strip()
            username = self.username_input.text().strip()
            password = self.password_input.text().strip()

            if not all([ip, port, username, password]):
                self.log("Error: Missing VPS credentials for refreshing.", "red")
                return

            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(ip, port=int(port), username=username, password=password, timeout=10)
            sftp = ssh.open_sftp()

            # Check if the current base path exists
            try:
                sftp.chdir(self.remote_base_path)
            except IOError:
                # Current directory doesn't exist, navigate to parent
                parent_path = os.path.dirname(self.remote_base_path)
                if parent_path == self.remote_base_path:
                    # Reached root, cannot go up
                    self.log("Error: Unable to navigate to parent directory.", "red")
                    sftp.close()
                    ssh.close()
                    return
                self.remote_base_path = parent_path
                self.dest_path.setText(self.remote_base_path)
                self.log(f"Current directory deleted. Navigated to parent directory: {self.remote_base_path}", "yellow")

            # Reload the remote directory
            self.remote_model.removeRows(0, self.remote_model.rowCount())
            self.populate_remote_tree(sftp, self.remote_base_path, self.remote_model.invisibleRootItem())

            sftp.close()
            ssh.close()

            self.log(f"Refreshed remote directory: {self.remote_base_path}", "green")
        except Exception as e:
            self.log(f"Error refreshing remote directory: {str(e)}", "red")


def main():
    app = QApplication(sys.argv)
    window = FileTransferApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
