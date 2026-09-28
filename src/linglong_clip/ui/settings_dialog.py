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

    def __init__(
        self,
        config,
        automation=None,
        device_status: Dict[str, object] = None,
        parent=None,
        native_report=None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._automation = automation
        self._device_status = device_status or {}
        #: `utils.selfcheck.SelfCheckReport`，为 None 时跳过原生能力面板
        self._native_report = native_report
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
        self._max_length.setRange(0, 5_000_000)
        self._max_length.setSpecialValueText("不截断")
        self._max_length.setSingleStep(10_000)
        self._max_length.setValue(int(self._config.get("capture.max_text_length", 200_000)))
        self._max_length.setToolTip(
            "超过此长度的文本只保留开头，原文长度记在记录的「被截断」标记里；\n"
            "0 表示不截断。截断发生在隐私判定之后，不会漏检藏在长文本里的敏感信息"
        )
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

        # 判重前先做归一的开关。单独放出来是因为它会改变「相同」的定义：
        # 关掉后同一页面被不同渠道分享出的链接会各占一行，属于用户可见行为。
        self._canonical_url = QCheckBox("链接去掉跟踪参数后判重（utm_*、fbclid 等）")
        self._canonical_url.setChecked(
            bool(self._config.get("history.canonical_dedupe.url", True))
        )
        history_form.addRow("", self._canonical_url)

        self._canonical_path = QCheckBox("路径收敛写法后判重（折叠 //、消解 . 与 ..）")
        self._canonical_path.setChecked(
            bool(self._config.get("history.canonical_dedupe.path", True))
        )
        history_form.addRow("", self._canonical_path)

        self._pin_immune = QCheckBox("固定项不受清理影响")
        self._pin_immune.setChecked(bool(self._config.get("history.pin_immune", True)))
        self._pin_immune.setToolTip(
            "勾选：固定项完全不占上面两个上限的额度；\n"
            "取消：固定项也计入上限（排序仍排前，所以先被清理的是久未使用的普通记录）"
        )
        history_form.addRow("", self._pin_immune)

        layout.addWidget(self._build_ocr_group())

        layout.addWidget(self._build_native_history_group())

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
        # 手填语言时立刻重算可用性：否则用户改完要等保存后才看得出没这语言
        self._ocr_languages.textChanged.connect(self._refresh_ocr_status)

        return group

    def _refresh_ocr_status(self) -> None:
        """探测当前环境是否真的能做 OCR，把结论直白地告诉用户。

        关键的一点：**不能只显示「已安装语言」**。此前正是这样做的，于是
        界面写着「可用 · 已安装语言：chi_sim、eng」，而实际请求的是 locale
        推断出的 `jpn` —— tesseract 遇到未安装的语言会整批失败，
        每一张截图都识别不出东西，界面上却毫无异常。
        所以这里要显示的是「实际会用哪个语言」，以及它与期望的差异。
        """
        if not self._ocr_enabled.isChecked():
            self._ocr_status.setText("已关闭（截图只记录图片本身）")
            return
        try:
            from ..core.ocr import TesseractBackend, select_languages

            backend = TesseractBackend()
            if not backend.available():
                self._ocr_status.setText("不可用 · 未找到 tesseract（截图将无法被文字搜索）")
                return

            installed = list(backend.languages())
            requested = self._ocr_languages.text().strip()
            locale_hint = str(self._config.get("ui.language", "zh_CN") or "zh_CN")
            choice = select_languages(installed, locale_hint, requested)

            if not choice.usable:
                self._ocr_status.setText(
                    "不可用 · tesseract 未安装任何语言数据，请先装语言包"
                    "（如 tesseract-ocr-chi-sim）"
                )
                return

            langs = "、".join(installed) or "未知"
            if choice.degraded:
                # 明确告知「能用但不是你要的」，别让用户以为是识别精度问题
                self._ocr_status.setText(
                    f"⚠ {choice.describe()} · 实际使用：{choice.languages} · "
                    f"已安装语言：{langs}"
                )
            else:
                self._ocr_status.setText(
                    f"可用 · 识别语言：{choice.languages} · 已安装语言：{langs}"
                )
        except Exception:  # noqa: BLE001 - 状态展示失败不该挡住设置页
            self._ocr_status.setText("无法探测（不影响其它功能）")

    def _build_native_history_group(self) -> QGroupBox:
        """系统剪贴板历史导入：补齐本工具启动之前复制的记录。

        本工具只看得到自己启动之后的内容；装好之前、或者被退出期间
        复制的东西，历史里就是缺的。Windows 10 1809+ 的系统剪贴板历史
        正好覆盖这段空缺。
        """
        group = QGroupBox("系统剪贴板历史")
        form = QFormLayout(group)

        self._native_import_button = QPushButton("导入系统历史…")
        self._native_import_button.clicked.connect(self._run_native_history_import)
        form.addRow("", self._native_import_button)

        self._native_import_status = QLabel()
        self._native_import_status.setWordWrap(True)
        form.addRow("状态", self._native_import_status)
        self._refresh_native_history_state()

        return group

    def _refresh_native_history_state(self) -> None:
        """探测当前平台到底能不能读系统历史，直白地写出来。"""
        try:
            from ..platform import detect_backend

            backend = detect_backend()
            supported = bool(getattr(backend, "is_history_supported", lambda: False)())
        except Exception:  # noqa: BLE001 - 探测失败不该挡住设置页
            supported = False

        if supported:
            self._native_import_status.setText(
                "可用 · 导入的内容会与实时采集走同一套隐私判定"
            )
            self._native_import_button.setEnabled(True)
        else:
            self._native_import_status.setText(
                "不可用 · 需要 Windows 10 1809+ 且在「设置 → 系统 → 剪贴板」"
                "里打开历史（其它平台由系统决定，程序侧无法补齐）"
            )
            self._native_import_button.setEnabled(False)

    def _run_native_history_import(self) -> None:
        """真的执行一次导入，并把结果（含被拦下的条数）摊给用户看。"""
        from ..core.collector import Collector
        from ..core.native_history import import_native_history
        from ..core.ocr import build_engine as build_ocr_engine
        from ..core.privacy import PrivacyGuard
        from ..platform import detect_backend

        try:
            from ..core.repository import open_repository

            storage = open_repository(self._config)
        except Exception as exc:  # noqa: BLE001
            self._native_import_status.setText(f"打不开历史仓库：{exc}")
            return

        try:
            collector = Collector(
                storage,
                self._config,
                PrivacyGuard(self._config),
                ocr=build_ocr_engine(self._config),
            )
            report = import_native_history(detect_backend(), collector)
        finally:
            storage.close()

        if not report.supported:
            self._native_import_status.setText(report.unsupported_reason)
            return
        text = report.summary()
        if report.skipped_privacy:
            text += "；被拦下的敏感内容已丢弃，未入库"
        self._native_import_status.setText(text)

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
        self._auto_clear.setToolTip(
            "把敏感记录复制出去后，若剪贴板里仍是这份内容就自动清空。\n"
            "只在内容未被替换时清，不会误删你后来的复制。\n"
            "注意：这只是缩小暴露窗口，不是擦除痕迹 —— 剪贴板管理器、"
            "输入法可能已经读过一次。低于 5 秒按「不清空」处理，"
            "否则粘贴还没发生内容就没了。"
        )
        form2.addRow("剪贴板自动清空", self._auto_clear)

        self._auto_clear_hint = QLabel()
        self._auto_clear_hint.setWordWrap(True)
        self._auto_clear_hint.setObjectName("hintLabel")
        form2.addRow("", self._auto_clear_hint)
        # 改了就要立刻看到结果，不用等保存 —— 否则用户无法判断自己改的有没有生效
        self._auto_clear.valueChanged.connect(self._refresh_outbound_hint)
        self._refresh_outbound_hint()

        self._redact = QCheckBox("复制到外部时对敏感片段脱敏")
        self._redact.setChecked(bool(self._config.get("privacy.redact_on_copy_out", False)))
        form2.addRow("", self._redact)

        layout.addWidget(group2)
        layout.addStretch(1)
        return page

    def _refresh_outbound_hint(self, *_args) -> None:
        """说清这台机器上「复制敏感内容」实际能做什么。

        此前这里只有一个勾选框和一句「自动清空」，而配置从头到尾没人读 ——
        用户看到的是「已设置」，实际什么都没发生。所以状态必须写出来。
        """
        parts = []
        if self._history_exclusion_formats():
            parts.append("系统级：复制敏感内容时不写入系统剪贴板历史（Win+V）")
        else:
            parts.append("本平台没有「不写入系统剪贴板历史」的机制，改用限时清空")
        seconds = self._auto_clear.value()
        if seconds > 0:
            parts.append(f"复制后 {seconds} 秒清空剪贴板（内容未被替换时）")
        else:
            parts.append("自动清空已关闭，复制出去的敏感内容会一直留在剪贴板")
        self._auto_clear_hint.setText("\n".join(f"· {item}" for item in parts))

    def _history_exclusion_formats(self):
        """当前平台是否提供写入级的剪贴板历史排除。

        默认自己探一次后端；测试可注入 `_exclusion_formats_override`
        来覆盖 —— 否则在 Linux 上永远看不到 Windows 那一条。
        """
        override = getattr(self, "_exclusion_formats_override", None)
        if override is not None:
            return list(override)
        try:
            from ..platform import detect_backend

            getter = getattr(detect_backend(), "history_exclusion_formats", None)
            if getter is None:
                return []
            return [str(item) for item in (getter() or []) if item]
        except Exception:  # noqa: BLE001 - 探测失败不该让设置页打不开
            return []

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
            details = self._device_status.get("device_details") or []
            if details:
                synced = [d for d in details if d.get("synced")]
                info_form.addRow(
                    "增量进度",
                    QLabel(
                        f"{len(synced)}/{len(details)} 个来源已同步过"
                        if details
                        else "暂无"
                    ),
                )
            peers = self._device_status.get("peers") or []
            if peers:
                online = [peer for peer in peers if peer.get("ok")]
                info_form.addRow(
                    "可同步对端",
                    QLabel(
                        f"{len(online)}/{len(peers)} 台在线 · "
                        + "、".join(
                            f"{peer.get('host')}:{peer.get('port')}"
                            + (f"（{peer['device']}）" if peer.get("device") else "")
                            for peer in peers
                        )
                    ),
                )
            cursor_file = str(self._device_status.get("cursor_file", "") or "")
            if cursor_file:
                label = QLabel(cursor_file)
                label.setToolTip("记录每个同步来源已拉到的时间点；删除它只会导致下次全量重扫")
                info_form.addRow("游标文件", label)
            layout.addWidget(info)

        layout.addWidget(self._build_flag_group())

        layout.addWidget(self._build_tombstone_group())

        layout.addStretch(1)
        return page

    def _count_pinned(self) -> int:
        """统计本机固定项条数，失败时返回 0。

        这个面板只是说明性质，不该因为它而在「历史文件损坏」时把设置页
        也一起弄崩 —— 那时用户恰恰最需要进设置。所以任何异常都退化为 0。
        """
        try:
            from ..core.repository import open_repository

            storage = open_repository(self._config)
        except Exception as exc:  # noqa: BLE001
            _log.debug("打开存储以统计固定项失败：%s", exc)
            return 0
        try:
            return sum(1 for item in storage.list(pinned_only=True, limit=100_000))
        except Exception as exc:  # noqa: BLE001
            _log.debug("统计固定项失败：%s", exc)
            return 0
        finally:
            try:
                storage.close()
            except Exception:  # noqa: BLE001
                pass

    def _build_flag_group(self) -> QGroupBox:
        """用户标记面板。

        为什么需要它：固定/收藏跨设备是**取并集**的，不在这里说清楚，
        用户会遇到「我在笔记本上取消了固定，怎么同步一轮又回来了」，
        并且完全无从判断这是设计还是缺陷。这类「用户无法从界面推断出
        的行为」必须写在界面上，而不是只留在 README 里。
        """
        group = QGroupBox("固定与收藏的同步")
        form = QFormLayout(group)

        pinned = self._count_pinned()
        form.addRow(
            "本机固定项",
            QLabel(f"{pinned} 条" if pinned else "暂无固定项"),
        )

        # QLabel 不吃 Markdown，这里不要写 **强调** 之类的标记 ——
        # 上一版写了，界面上直接显示成字面的星号，截图里一眼能看出来。
        hint = QLabel(
            "在一台设备上固定或收藏某条记录，其它设备下次同步也会带上这个标记。\n"
            "标记取并集：一台设过就一直保留，因此在另一台取消后，\n"
            "再同步一轮会重新出现 —— 因为无法区分「那边取消过」和「那边从没设过」。\n"
            "想彻底取消，请在每台设备上分别取消。"
        )
        hint.setWordWrap(True)
        form.addRow("", hint)
        return group

    def _build_tombstone_group(self) -> QGroupBox:
        """删除标记面板：让「我在这台删了，那台却还在」有地方可查。"""
        group = QGroupBox("删除的同步")
        form = QFormLayout(group)

        try:
            from ..core.tombstones import build_tombstone_store

            status = build_tombstone_store().status()
        except Exception as exc:  # noqa: BLE001 - 面板不该因为读不到文件而崩
            _log.debug("读取删除标记失败：%s", exc)
            status = {}

        count = int(status.get("count", 0) or 0)
        pending = int(status.get("pending", 0) or 0)
        if not count:
            summary = "本机还没有被删除的内容"
        elif pending:
            summary = f"共 {count} 条，{pending} 条等待同步到其它设备"
        else:
            summary = f"共 {count} 条，均已同步到其它设备"
        form.addRow("状态", QLabel(summary))

        retention = int(status.get("retention_seconds", 0) or 0)
        if retention:
            days = retention / 86400
            form.addRow(
                "保留期",
                QLabel(
                    f"{days:g} 天（超期后不再向对端传播，避免误删对端自己的数据）"
                ),
            )

        path = str(status.get("path", "") or "")
        if path:
            label = QLabel(path)
            label.setToolTip(
                "记录本机删除过的内容指纹（不含内容本身）。\n"
                "删掉这个文件只会让已发出的删除不再复述，不影响本地删除。"
            )
            form.addRow("标记文件", label)

        hint = QLabel(
            "在一台设备上删除记录后，其它设备上的同一内容会在下次同步时一起删除；\n"
            "固定项与「本轮之前就存在很久」的删除会被保留。"
        )
        hint.setWordWrap(True)
        form.addRow("", hint)
        return group

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
        self._fuzzy.setToolTip(
            "开启后搜索框会解析「上周的截图」这类自然语言并回显理解结果；\n"
            "关闭后查询串原样做全文检索"
        )
        form.addRow("", self._fuzzy)

        self._density = QComboBox()
        # data 存配置值，显示的是人话
        self._density.addItem("舒适（三行信息）", "comfortable")
        self._density.addItem("紧凑（两行，一屏更多）", "compact")
        current_density = str(self._config.get("ui.card_density", "comfortable") or "")
        index = self._density.findData(current_density)
        self._density.setCurrentIndex(index if index >= 0 else 0)
        self._density.setToolTip("只影响列表显示密度，不影响采集与检索结果")
        form.addRow("列表密度", self._density)

        self._hotkey = QLineEdit(str(self._config.get("ui.global_hotkey", "Ctrl+Shift+V")))
        form.addRow("唤起快捷键", self._hotkey)

        self._quick_hotkey = QLineEdit(
            str(self._config.get("ui.quick_paste_hotkey", "Ctrl+Shift+1..9"))
        )
        self._quick_hotkey.setToolTip(
            "按序号直接粘贴第 N 条，如 Ctrl+Shift+1..9 会注册 9 个快捷键。\n"
            "改前缀只需改这一处；有一个注册失败（被占用）时整组不启用，\n"
            "不会出现「1、2 能用、3 不行」。留空表示不注册。"
        )
        form.addRow("快速粘贴序号", self._quick_hotkey)

        self._start_minimized = QCheckBox("启动时最小化到托盘")
        self._start_minimized.setChecked(bool(self._config.get("ui.start_minimized", True)))
        self._start_minimized.setToolTip("命令行 `--minimized` 可临时覆盖此项")
        form.addRow("", self._start_minimized)

        self._close_to_tray = QCheckBox("关闭窗口时保留后台运行")
        self._close_to_tray.setChecked(bool(self._config.get("ui.close_to_tray", True)))
        self._close_to_tray.setToolTip(
            "勾选：点关闭只收进托盘，采集继续；\n"
            "取消：点关闭直接退出程序（系统托盘不可用时一律退出）"
        )
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

        if self._native_report is not None:
            layout.addWidget(self._build_native_group())
        layout.addStretch(1)
        return page

    def _build_native_group(self) -> QWidget:
        """原生能力面板。

        把 `--selfcheck` 的结论搬进界面：用户不该为了知道
        「全局快捷键为什么没生效」去翻日志或开终端。
        """
        from ..utils.selfcheck import DEGRADED, FAIL

        group = QGroupBox("原生能力")
        form = QFormLayout(group)
        report = self._native_report

        form.addRow("自检结论", QLabel(report.summary))
        for item in report.results:
            value = QLabel(item.detail or "-")
            value.setWordWrap(True)
            if item.status == FAIL:
                value.setStyleSheet("color:#e5484d")
                value.setText(f"{item.detail}\n{item.hint}")
            elif item.status == DEGRADED:
                value.setStyleSheet("color:#e5a00d")
                value.setText(f"{item.detail}\n{item.hint}")
            form.addRow(item.name, value)

        hint = QLabel(
            "<p style='color:#8d96a8'>命令行可随时复查："
            "<code>linglong-clipboard --selfcheck</code></p>"
        )
        hint.setTextFormat(Qt.TextFormat.RichText)
        hint.setWordWrap(True)
        form.addRow("", hint)
        return group

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
            "history.canonical_dedupe.url": self._canonical_url.isChecked(),
            "history.canonical_dedupe.path": self._canonical_path.isChecked(),
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
            "ui.card_density": self._density.currentData(),
            "ui.global_hotkey": self._hotkey.text().strip(),
            "ui.quick_paste_hotkey": self._quick_hotkey.text().strip(),
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
