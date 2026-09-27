"""设置对话框。

按「采集 / 隐私 / 同步 / 外观 / 自动化」分组。
改动立即写回配置对象，由调用方决定何时生效（部分项需要重启监听线程）。
"""

from __future__ import annotations

from typing import Dict

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..core.automation import Rule
from ..core.models import KIND_LABELS
from ..utils.logger import get_logger

_log = get_logger("settings")

#: 可勾选的采集类型
CAPTURE_TYPES = ["text", "rich", "image", "file", "link", "color", "code"]


class SettingsDialog(QDialog):
    """配置编辑器。`changes` 里是本次被改动的配置键。"""

    def __init__(self, config, automation=None, device_status: Dict[str, object] = None, parent=None) -> None:
        super().__init__(parent)
        self._config = config
        self._automation = automation
        self._device_status = device_status or {}
        self.changes: Dict[str, object] = {}

        self.setWindowTitle("设置 — 玲珑剪切板")
        self.resize(640, 560)

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.addTab(self._build_capture_tab(), "采集")
        tabs.addTab(self._build_privacy_tab(), "隐私")
        tabs.addTab(self._build_sync_tab(), "同步")
        tabs.addTab(self._build_ui_tab(), "外观")
        tabs.addTab(self._build_automation_tab(), "自动化")
        tabs.addTab(self._build_about_tab(), "关于")
        layout.addWidget(tabs)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ---- 各标签页 ----

    def _build_capture_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        group = QGroupBox("采集范围")
        form = QFormLayout(group)

        self._capture_enabled = QCheckBox("启用剪贴板采集")
        self._capture_enabled.setChecked(bool(self._config.get("capture.enabled", True)))
        form.addRow("", self._capture_enabled)

        self._type_boxes: Dict[str, QCheckBox] = {}
        type_row = QWidget()
        type_layout = QHBoxLayout(type_row)
        type_layout.setContentsMargins(0, 0, 0, 0)
        enabled_types = set(self._config.get("capture.types", []) or [])
        for kind in CAPTURE_TYPES:
            box = QCheckBox(KIND_LABELS.get(kind, kind))
            box.setChecked(kind in enabled_types)
            type_layout.addWidget(box)
            self._type_boxes[kind] = box
        type_layout.addStretch(1)
        form.addRow("采集类型", type_row)

        self._poll_interval = QSpinBox()
        self._poll_interval.setRange(100, 5000)
        self._poll_interval.setSingleStep(50)
        self._poll_interval.setSuffix(" ms")
        self._poll_interval.setValue(int(self._config.get("capture.poll_interval_ms", 300)))
        form.addRow("轮询间隔", self._poll_interval)

        self._max_length = QSpinBox()
        self._max_length.setRange(1000, 5_000_000)
        self._max_length.setSingleStep(10_000)
        self._max_length.setValue(int(self._config.get("capture.max_text_length", 200_000)))
        form.addRow("单条最大字符数", self._max_length)

        layout.addWidget(group)

        history_group = QGroupBox("历史保留")
        history_form = QFormLayout(history_group)

        self._max_items = QSpinBox()
        self._max_items.setRange(0, 100_000)
        self._max_items.setSingleStep(100)
        self._max_items.setSpecialValueText("不限制")
        self._max_items.setValue(int(self._config.get("history.max_items", 2000)))
        history_form.addRow("最多保留条数", self._max_items)

        self._max_days = QSpinBox()
        self._max_days.setRange(0, 3650)
        self._max_days.setSpecialValueText("不限制")
        self._max_days.setValue(int(self._config.get("history.max_days", 0)))
        history_form.addRow("最多保留天数", self._max_days)

        self._merge_dup = QCheckBox("相同内容只保留一条（记录复制次数）")
        self._merge_dup.setChecked(bool(self._config.get("history.merge_duplicates", True)))
        history_form.addRow("", self._merge_dup)

        self._pin_immune = QCheckBox("固定项不受清理影响")
        self._pin_immune.setChecked(bool(self._config.get("history.pin_immune", True)))
        history_form.addRow("", self._pin_immune)

        layout.addWidget(self._build_ocr_group())

        layout.addWidget(history_group)
        layout.addWidget(self._build_storage_group())
        layout.addStretch(1)
        return page

    def _build_ocr_group(self) -> QGroupBox:
        """截图 OCR：图片能被文字搜到。默认开启，缺 tesseract 时自动降级。"""
        group = QGroupBox("截图文字识别（OCR）")
        form = QFormLayout(group)

        self._ocr_enabled = QCheckBox("识别截图中的文字，让图片能被搜索到")
        self._ocr_enabled.setChecked(bool(self._config.get("ocr.enabled", True)))
        self._ocr_enabled.setToolTip(
            "需要系统安装 tesseract。未安装时静默跳过，图片照常记录，只是搜不到文字。"
        )
        form.addRow("", self._ocr_enabled)

        self._ocr_languages = QLineEdit(str(self._config.get("ocr.languages", "") or ""))
        self._ocr_languages.setPlaceholderText("留空按界面语言推断，多语言用 + 连接，如 chi_sim+eng")
        form.addRow("识别语言", self._ocr_languages)

        self._ocr_min_confidence = QSpinBox()
        self._ocr_min_confidence.setRange(0, 100)
        self._ocr_min_confidence.setSuffix(" %")
        self._ocr_min_confidence.setValue(int(self._config.get("ocr.min_confidence", 0) or 0))
        self._ocr_min_confidence.setToolTip("低于该置信度的识别结果直接丢弃，避免把噪音写进历史。")
        form.addRow("最低置信度", self._ocr_min_confidence)

        self._ocr_status = QLabel()
        self._ocr_status.setWordWrap(True)
        form.addRow("状态", self._ocr_status)
        self._refresh_ocr_status()
        self._ocr_enabled.toggled.connect(self._refresh_ocr_status)

        return group

    def _refresh_ocr_status(self) -> None:
        """探测当前环境是否真的能做 OCR，把结论直白地告诉用户。"""
        if not self._ocr_enabled.isChecked():
            self._ocr_status.setText("已关闭（截图只记录图片本身）")
            return
        try:
            from ..core.ocr import TesseractBackend

            backend = TesseractBackend()
            if backend.available():
                langs = "、".join(backend.languages()) or "未知"
                self._ocr_status.setText(f"可用 · 已安装语言：{langs}")
            else:
                self._ocr_status.setText("不可用 · 未找到 tesseract（截图将无法被文字搜索）")
        except Exception:  # noqa: BLE001 - 状态展示失败不该挡住设置页
            self._ocr_status.setText("无法探测（不影响其它功能）")

    def _build_privacy_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        group = QGroupBox("来源过滤")
        form = QFormLayout(group)

        self._exclude_pm = QCheckBox("跳过密码管理器（1Password、Bitwarden、KeePass 等）")
        self._exclude_pm.setChecked(bool(self._config.get("privacy.exclude_password_managers", True)))
        form.addRow("", self._exclude_pm)

        self._blocked_apps = QLineEdit(
            ", ".join(self._config.get("privacy.blocked_apps", []) or [])
        )
        self._blocked_apps.setPlaceholderText("用英文逗号分隔，例如 keychain, banking-app")
        form.addRow("额外屏蔽应用", self._blocked_apps)
        layout.addWidget(group)

        group2 = QGroupBox("敏感内容")
        form2 = QFormLayout(group2)

        self._sensitive_retention = QSpinBox()
        self._sensitive_retention.setRange(0, 86_400 * 30)
        self._sensitive_retention.setSingleStep(60)
        self._sensitive_retention.setSuffix(" 秒")
        self._sensitive_retention.setSpecialValueText("完全不记录（推荐）")
        self._sensitive_retention.setValue(
            int(self._config.get("privacy.sensitive_retention_seconds", 0) or 0)
        )
        self._sensitive_retention.setToolTip(
            "设为 0 时，识别出密码、密钥等内容后直接丢弃，不写入历史。"
        )
        form2.addRow("敏感内容保留时长", self._sensitive_retention)

        self._auto_clear = QSpinBox()
        self._auto_clear.setRange(0, 3600)
        self._auto_clear.setSuffix(" 秒")
        self._auto_clear.setSpecialValueText("不自动清除")
        self._auto_clear.setValue(int(self._config.get("privacy.sensitive_auto_clear_seconds", 30)))
        self._auto_clear.setToolTip("安全粘贴后，若剪贴板中的敏感内容超时未被替换则自动清空。")
        form2.addRow("剪贴板自动清空", self._auto_clear)

        self._redact = QCheckBox("复制到外部时对敏感片段脱敏")
        self._redact.setChecked(bool(self._config.get("privacy.redact_on_copy_out", False)))
        form2.addRow("", self._redact)

        layout.addWidget(group2)
        layout.addStretch(1)
        return page

    def _build_sync_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        group = QGroupBox("跨设备同步")
        form = QFormLayout(group)

        self._sync_enabled = QCheckBox("启用同步")
        self._sync_enabled.setChecked(bool(self._config.get("sync.enabled", False)))
        form.addRow("", self._sync_enabled)

        self._sync_folder = QLineEdit(str(self._config.get("sync.folder", "") or ""))
        self._sync_folder.setPlaceholderText("共享文件夹路径，例如 OneDrive/Sync/Linglong")
        form.addRow("同步目录", self._sync_folder)

        self._device_name = QLineEdit(str(self._config.get("sync.device_name", "") or ""))
        self._device_name.setPlaceholderText("留空则用主机名")
        form.addRow("本机名称", self._device_name)

        self._sync_encrypt = QCheckBox("加密同步内容")
        self._sync_encrypt.setChecked(bool(self._config.get("sync.encrypt", True)))
        self._sync_encrypt.setToolTip("需要安装 cryptography；未安装时自动退化为明文。")
        form.addRow("", self._sync_encrypt)

        layout.addWidget(group)

        layout.addWidget(self._build_lan_group())

        if self._device_status:
            info = QGroupBox("状态")
            info_form = QFormLayout(info)
            info_form.addRow("同步目录", QLabel(str(self._device_status.get("folder", "-"))))
            devices = self._device_status.get("devices") or []
            info_form.addRow(
                "已发现的设备",
                QLabel("、".join(devices) if devices else "暂无（其它设备尚未导出记录）"),
            )
            info_form.addRow(
                "加密", QLabel("已启用" if self._device_status.get("encrypted") else "未启用")
            )
            layout.addWidget(info)

        layout.addStretch(1)
        return page

    def _build_lan_group(self) -> QGroupBox:
        """局域网直连：同网段设备互相拉取，不经过任何服务器。"""
        group = QGroupBox("局域网直连")
        form = QFormLayout(group)

        self._sync_mode = QComboBox()
        self._sync_mode.addItem("共享文件夹（配合 OneDrive / iCloud / Syncthing）", "folder")
        self._sync_mode.addItem("局域网直连（同网段设备互连，不经服务器）", "lan")
        current = str(self._config.get("sync.mode", "folder") or "folder")
        index = max(0, self._sync_mode.findData(current))
        self._sync_mode.setCurrentIndex(index)
        form.addRow("同步方式", self._sync_mode)

        self._lan_token = QLineEdit(str(self._config.get("sync.lan_token", "") or ""))
        self._lan_token.setEchoMode(QLineEdit.Password)
        self._lan_token.setPlaceholderText("设备间共享口令，留空表示不鉴权（仅限可信内网）")
        self._lan_token.setToolTip("同网段不等于可信：不设口令时，同网络内任何人都能读取你的剪贴板。")
        form.addRow("共享口令", self._lan_token)

        self._lan_port = QSpinBox()
        self._lan_port.setRange(0, 65535)
        self._lan_port.setValue(int(self._config.get("sync.lan_port", 48620) or 0))
        self._lan_port.setToolTip("监听端口，0 表示由系统分配。多台设备需用相同端口才能互连。")
        form.addRow("监听端口", self._lan_port)

        peers = self._config.get("sync.lan_peers", []) or []
        if isinstance(peers, str):
            peers = [line for line in peers.splitlines() if line.strip()]
        self._lan_peers = QPlainTextEdit("\n".join(str(peer) for peer in peers))
        self._lan_peers.setPlaceholderText("对端地址，一行一个\n例如：\n192.168.1.8\n192.168.1.9:48620")
        self._lan_peers.setFixedHeight(80)
        form.addRow("对端地址", self._lan_peers)

        self._lan_discovery = QCheckBox("自动发现同网段设备（UDP 广播）")
        self._lan_discovery.setChecked(bool(self._config.get("sync.lan_discovery", False)))
        self._lan_discovery.setToolTip("部分 Wi-Fi 会拦组播/广播，发现不到时请手动填对端地址。")
        form.addRow("", self._lan_discovery)

        self._lan_status = QLabel()
        self._lan_status.setWordWrap(True)
        form.addRow("状态", self._lan_status)
        self._sync_mode.currentIndexChanged.connect(self._refresh_lan_state)
        self._sync_enabled.toggled.connect(self._refresh_lan_state)
        self._refresh_lan_state()

        return group

    def _refresh_lan_state(self) -> None:
        """局域网相关输入只在选中「局域网直连」且启用同步时才可用。"""
        is_lan = self._sync_mode.currentData() == "lan"
        for widget in (
            self._sync_folder,
            self._lan_token,
            self._lan_port,
            self._lan_peers,
            self._lan_discovery,
        ):
            widget.setEnabled(is_lan or widget is self._sync_folder)

        if is_lan:
            self._lan_status.setText(
                "已选择局域网直连 · 本机监听 %d 端口" % self._lan_port.value()
            )
        else:
            self._lan_status.setText("当前使用共享文件夹模式，局域网选项不生效")
        self._lan_port.setEnabled(is_lan)
        self._lan_token.setEnabled(is_lan)
        self._lan_peers.setEnabled(is_lan)
        self._lan_discovery.setEnabled(is_lan)

    def _build_ui_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        group = QGroupBox("界面")
        form = QFormLayout(group)

        self._theme = QComboBox()
        self._theme.addItems(["system", "dark", "light"])
        self._theme.setCurrentText(str(self._config.get("ui.theme", "system")))
        form.addRow("主题", self._theme)

        self._fuzzy = QCheckBox("启用语义检索与建议")
        self._fuzzy.setChecked(bool(self._config.get("ui.fuzzy_search", True)))
        form.addRow("", self._fuzzy)

        self._hotkey = QLineEdit(str(self._config.get("ui.global_hotkey", "Ctrl+Shift+V")))
        form.addRow("唤起快捷键", self._hotkey)

        self._quick_hotkey = QLineEdit(
            str(self._config.get("ui.quick_paste_hotkey", "Ctrl+Shift+1..9"))
        )
        self._quick_hotkey.setEnabled(False)
        self._quick_hotkey.setToolTip("快速粘贴序号段固定，仅作说明")
        form.addRow("快速粘贴序号", self._quick_hotkey)

        self._start_minimized = QCheckBox("启动时最小化到托盘")
        self._start_minimized.setChecked(bool(self._config.get("ui.start_minimized", True)))
        form.addRow("", self._start_minimized)

        self._close_to_tray = QCheckBox("关闭窗口时保留后台运行")
        self._close_to_tray.setChecked(bool(self._config.get("ui.close_to_tray", True)))
        form.addRow("", self._close_to_tray)

        layout.addWidget(group)
        layout.addStretch(1)
        return page

    def _build_automation_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        hint = QLabel(
            "规则用 JSON 描述。`trigger` 为 `auto` 时会在采集后自动执行转换；\n"
            "`actions` 可写多个动作，会按顺序链式执行。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._rules_editor = QPlainTextEdit()
        if self._automation is not None:
            import json

            self._rules_editor.setPlainText(
                json.dumps(self._automation.to_config(), ensure_ascii=False, indent=2)
            )
        else:
            self._rules_editor.setPlainText("[]")
        layout.addWidget(self._rules_editor, 1)

        row = QHBoxLayout()
        template_button = QPushButton("插入示例")
        template_button.clicked.connect(self._insert_template)
        row.addWidget(template_button)
        row.addStretch(1)
        layout.addLayout(row)
        return page

    def _build_storage_group(self) -> QGroupBox:
        """历史存储方式：SQLite 数据库或 .clipboard/clipboard.json。

        默认仍是 SQLite；JSON 单文件适合「想看数据长什么样」「拷一个文件搬机器」的场景，
        代价是记录量大时每次写入都要重写整份文件。
        """
        from ..utils.paths import data_dir, database_path, json_path

        group = QGroupBox("历史存储")
        form = QFormLayout(group)

        self._storage_backend = QComboBox()
        self._storage_backend.addItem("SQLite 数据库（默认，记录多时更合适）", "sqlite")
        self._storage_backend.addItem("JSON 单文件（.clipboard/clipboard.json，可读可搬）", "json")
        current = str(self._config.get("storage.backend", "sqlite") or "sqlite")
        self._storage_backend.setCurrentIndex(max(0, self._storage_backend.findData(current)))
        form.addRow("存储方式", self._storage_backend)

        self._storage_inline = QCheckBox("图片以 data URI 内联进 JSON 文件")
        self._storage_inline.setChecked(bool(self._config.get("storage.json_inline_images", True)))
        self._storage_inline.setToolTip("内联后单个文件即可搬走，代价是文件变大（约 +33%）。")
        form.addRow("", self._storage_inline)

        self._storage_limit = QSpinBox()
        self._storage_limit.setRange(64, 32 * 1024)
        self._storage_limit.setSingleStep(256)
        self._storage_limit.setSuffix(" KB")
        self._storage_limit.setValue(int(self._config.get("storage.json_max_inline_image_kb", 2048) or 2048))
        self._storage_limit.setToolTip("超过该大小的图片仍存为 images/ 下的独立文件。")
        form.addRow("内联图片上限", self._storage_limit)

        path_label = QLabel(
            f"数据目录：{data_dir()}\n"
            f"SQLite：{database_path().name}　·　JSON：{json_path()}"
        )
        path_label.setWordWrap(True)
        path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow("存放位置", path_label)

        self._storage_hint = QLabel()
        self._storage_hint.setWordWrap(True)
        form.addRow("", self._storage_hint)
        self._storage_backend.currentIndexChanged.connect(self._refresh_storage_hint)
        self._refresh_storage_hint()

        self._storage_inline.setEnabled(self._storage_backend.currentData() == "json")
        self._storage_limit.setEnabled(self._storage_backend.currentData() == "json")
        return group

    def _refresh_storage_hint(self) -> None:
        is_json = self._storage_backend.currentData() == "json"
        self._storage_inline.setEnabled(is_json)
        self._storage_limit.setEnabled(is_json)
        if is_json:
            self._storage_hint.setText(
                "切换后重启生效。已有记录可用命令行搬运："
                "linglong-clipboard --storage-migrate（原数据保留，不会丢）。"
            )
        else:
            self._storage_hint.setText(
                "SQLite 仍是默认选择：记录量大、检索快。"
                "想看数据长什么样或整份搬走时，再切到 JSON。"
            )

    def _build_about_tab(self) -> QWidget:
        from .. import __version__
        from ..utils.paths import database_path, json_path

        backend = str(self._config.get("storage.backend", "sqlite") or "sqlite")
        self._storage_location = str(json_path() if backend == "json" else database_path())

        page = QWidget()
        layout = QVBoxLayout(page)

        import platform as _platform

        import sys

        text = QLabel(
            f"<h3>玲珑剪切板 {__version__}</h3>"
            "<p>跨平台智能剪贴板：本地优先、语义检索、可编程转换。</p>"
            "<p style='color:#8d96a8'>"
            f"运行环境：{_platform.platform()}<br>"
            f"Python {sys.version.split()[0]}<br>"
            f"配置目录：{self._config.path.parent}<br>"
            f"历史文件：{self._storage_location}"
            "</p>"
        )
        text.setWordWrap(True)
        text.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(text)
        layout.addStretch(1)
        return page

    # ---- 保存 ----

    def _insert_template(self) -> None:
        import json

        template = [
            Rule(
                name="自动格式化 JSON",
                kinds=["code", "text"],
                pattern=r"^\s*[\{\[]",
                min_length=20,
                actions=["json.format"],
                trigger="auto",
            ).to_dict(),
            Rule(
                name="粘贴前去掉链接跟踪参数",
                kinds=["link"],
                contains="utm_",
                actions=["url.strip_tracking"],
                trigger="manual",
            ).to_dict(),
        ]
        existing = self._rules_editor.toPlainText().strip()
        merged = json.loads(existing) if existing.startswith("[") and existing != "[]" else []
        for entry in template:
            if not any(rule.get("name") == entry["name"] for rule in merged):
                merged.append(entry)
        self._rules_editor.setPlainText(json.dumps(merged, ensure_ascii=False, indent=2))

    def _save(self) -> None:
        import json

        values: Dict[str, object] = {
            "capture.enabled": self._capture_enabled.isChecked(),
            "capture.types": [kind for kind, box in self._type_boxes.items() if box.isChecked()],
            "capture.poll_interval_ms": self._poll_interval.value(),
            "capture.max_text_length": self._max_length.value(),
            "history.max_items": self._max_items.value(),
            "history.max_days": self._max_days.value(),
            "history.merge_duplicates": self._merge_dup.isChecked(),
            "history.pin_immune": self._pin_immune.isChecked(),
            "privacy.exclude_password_managers": self._exclude_pm.isChecked(),
            "privacy.blocked_apps": [
                entry.strip()
                for entry in self._blocked_apps.text().replace("，", ",").split(",")
                if entry.strip()
            ],
            "privacy.sensitive_retention_seconds": self._sensitive_retention.value(),
            "privacy.sensitive_auto_clear_seconds": self._auto_clear.value(),
            "privacy.redact_on_copy_out": self._redact.isChecked(),
            "storage.backend": self._storage_backend.currentData(),
            "storage.json_inline_images": self._storage_inline.isChecked(),
            "storage.json_max_inline_image_kb": self._storage_limit.value(),
            "ocr.enabled": self._ocr_enabled.isChecked(),
            "ocr.languages": self._ocr_languages.text().strip(),
            "ocr.min_confidence": self._ocr_min_confidence.value(),
            "sync.enabled": self._sync_enabled.isChecked(),
            "sync.mode": self._sync_mode.currentData(),
            "sync.folder": self._sync_folder.text().strip(),
            "sync.device_name": self._device_name.text().strip(),
            "sync.encrypt": self._sync_encrypt.isChecked(),
            "sync.lan_token": self._lan_token.text().strip(),
            "sync.lan_port": self._lan_port.value(),
            "sync.lan_peers": [
                line.strip()
                for line in self._lan_peers.toPlainText().splitlines()
                if line.strip()
            ],
            "sync.lan_discovery": self._lan_discovery.isChecked(),
            "ui.theme": self._theme.currentText(),
            "ui.fuzzy_search": self._fuzzy.isChecked(),
            "ui.global_hotkey": self._hotkey.text().strip(),
            "ui.start_minimized": self._start_minimized.isChecked(),
            "ui.close_to_tray": self._close_to_tray.isChecked(),
        }

        if self._automation is not None:
            raw = self._rules_editor.toPlainText().strip() or "[]"
            try:
                parsed = json.loads(raw)
                if not isinstance(parsed, list):
                    raise ValueError("规则必须是 JSON 数组")
                self._automation.load_from_config(parsed)
                values["automation.workflows"] = parsed
            except (json.JSONDecodeError, ValueError) as exc:
                QMessageBox.warning(self, "规则格式错误", f"无法解析自动化规则：\n{exc}")
                return

        for key, value in values.items():
            if self._config.get(key) != value:
                self.changes[key] = value

        self._config.update(values)
        self.accept()
