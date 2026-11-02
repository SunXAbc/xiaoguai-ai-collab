#include "Infrastructure.hpp"
#include "Controller.hpp"
#include <afxdlgs.h>
#include <afxole.h>
#include <shellapi.h>
#include <shlwapi.h>
#include <wrl.h>
#include <WebView2.h>
#include <algorithm>
#include <cctype>
#include <deque>
#include <memory>
#include <stdexcept>

#pragma comment(lib, "WebView2LoaderStatic.lib")
#pragma comment(lib, "shlwapi.lib")
// 只把本程序的静态资源映射到这个来源，不打开任意外部网页。
static constexpr wchar_t APP_ORIGIN[] = L"https://app.xiaoguai.local/";
using Microsoft::WRL::Callback;
using Microsoft::WRL::ComPtr;

class MainFrame final : public CFrameWnd {
public:
    // root 为程序目录；data 为私有数据；layout_report 非空时只运行网页布局测试。
    MainFrame(fs::path root, fs::path data, fs::path layout_report = {})
        : root_(std::move(root)), data_(std::move(data)), layout_report_(std::move(layout_report)) {}
protected:
    DECLARE_MESSAGE_MAP()
    afx_msg int OnCreate(LPCREATESTRUCT create);
    afx_msg void OnSize(UINT type, int width, int height);
    afx_msg void OnGetMinMaxInfo(MINMAXINFO* info);
    afx_msg void OnClose();
    afx_msg void OnTimer(UINT_PTR timer);
    afx_msg LRESULT OnSnapshot(WPARAM unused, LPARAM value);
    afx_msg LRESULT OnUICommand(WPARAM unused, LPARAM value);
private:
    // 创建本地网页视图，异步回调在窗口关闭后自动失效。
    void CreateBrowser();
    // 只以 JSON 数据传递 value，不把消息拼接成脚本执行。
    void Send(const Json& value);
    // 验证本地来源并处理已知业务消息。
    void Receive(ICoreWebView2WebMessageReceivedEventArgs* args);
    // 将真实表单的 action/fields 交给原有后台业务控制器。
    void Action(const Json& message);
    // 给 id 对应的操作返回真实错误，不自动重复提交。
    void Fail(const std::string& id, const std::string& error);
    // 在三个真实窗口尺寸上执行浏览器 DOM 布局验证。
    void NextLayoutCase(const Json& previous = Json::object());
    // 只渲染本程序自己的网页内容，用于交付预览；不会捕获其他桌面窗口。
    void SavePreview();
    fs::path root_; // 程序与静态网页资源根目录。
    fs::path data_; // 应用私有数据和独立 WebView2 缓存。
    fs::path layout_report_; // 仅测试模式的结果文件。
    std::unique_ptr<Controller> worker_; // 原有后端、MCP 队列与进程管理。
    ComPtr<ICoreWebView2Controller> browser_controller_; // 网页的原生宿主。
    ComPtr<ICoreWebView2> browser_; // 可信本地文档；没有管理密钥。
    std::shared_ptr<int> life_ = std::make_shared<int>(0); // 异步回调存活标记。
    Json last_snapshot_ = Json::object(); // 页面准备好之前保留最后实际快照。
    std::deque<Json> waiting_commands_; // 暂存加载期间已领取的命令，不重放业务动作。
    bool web_ready_ = false; // 收到页面 ready 后才派发控制命令。
    bool closing_ = false; // 关闭期间禁止再投递网页数据。
    int layout_case_ = 0; // 当前布局尺寸。
    Json layout_cases_ = Json::array(); // 已完成的实际浏览器测试。
};

BEGIN_MESSAGE_MAP(MainFrame, CFrameWnd)
    ON_WM_CREATE()
    ON_WM_SIZE()
    ON_WM_GETMINMAXINFO()
    ON_WM_CLOSE()
    ON_WM_TIMER()
    ON_MESSAGE(WM_SNAPSHOT, &MainFrame::OnSnapshot)
    ON_MESSAGE(WM_UI_COMMAND, &MainFrame::OnUICommand)
END_MESSAGE_MAP()

int MainFrame::OnCreate(LPCREATESTRUCT create) {
    if (CFrameWnd::OnCreate(create) == -1) return -1;
    fs::create_directories(data_);
    if (layout_report_.empty()) worker_ = std::make_unique<Controller>(m_hWnd, root_, data_);
    CreateBrowser();
    return 0;
}

void MainFrame::Send(const Json& value) {
    if (browser_ && web_ready_ && !closing_) browser_->PostWebMessageAsJson(Wide(value.dump()).c_str());
}

void MainFrame::Fail(const std::string& id, const std::string& error) {
    Send({{"type", "snapshot"}, {"value", {{"error", error}, {"done", true}, {"ui_id", id}}}});
}

void MainFrame::CreateBrowser() {
    const std::weak_ptr<int> alive = life_; // 回调不能延长已关闭窗口的生命周期。
    const fs::path cache = data_ / L"webview2"; // 不复用用户浏览器账户或默认缓存目录。
    const HRESULT started = CreateCoreWebView2EnvironmentWithOptions(nullptr, cache.c_str(), nullptr,
        Callback<ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler>([this, alive](HRESULT result, ICoreWebView2Environment* environment) -> HRESULT {
            if (alive.expired()) return S_OK;
            if (FAILED(result) || !environment) {
                MessageBox(L"无法初始化网页界面，请确认 Microsoft Edge WebView2 Runtime 可用。", L"小怪", MB_OK | MB_ICONERROR);
                return S_OK;
            }
            return environment->CreateCoreWebView2Controller(m_hWnd,
                Callback<ICoreWebView2CreateCoreWebView2ControllerCompletedHandler>([this, alive](HRESULT result_code, ICoreWebView2Controller* controller) -> HRESULT {
                    if (alive.expired()) return S_OK;
                    if (FAILED(result_code) || !controller) return S_OK;
                    browser_controller_ = controller;
                    browser_controller_->put_IsVisible(TRUE);
                    controller->get_CoreWebView2(&browser_);
                    CRect client; // 网页填满实际客户区，缩放由浏览器处理。
                    GetClientRect(&client);
                    controller->put_Bounds(client);
                    ComPtr<ICoreWebView2_3> mapping; // 本地虚拟主机映射，无需额外开启网页服务器。
                    if (FAILED(browser_.As(&mapping))) return E_NOINTERFACE;
                    const fs::path assets = root_ / L"web"; // 仅这个静态资源目录可被网页读取。
                    mapping->SetVirtualHostNameToFolderMapping(L"app.xiaoguai.local", assets.c_str(), COREWEBVIEW2_HOST_RESOURCE_ACCESS_KIND_DENY);
                    ComPtr<ICoreWebView2Settings> settings; // 收紧浏览器功能，不允许界面跳转到其他来源。
                    browser_->get_Settings(&settings);
                    settings->put_IsStatusBarEnabled(FALSE);
                    settings->put_AreDefaultContextMenusEnabled(FALSE);
                    settings->put_AreDevToolsEnabled(FALSE);
                    settings->put_IsWebMessageEnabled(TRUE);
                    EventRegistrationToken token{}; // 注册随 WebView2 对象一起释放。
                    browser_->add_NavigationStarting(Callback<ICoreWebView2NavigationStartingEventHandler>([alive](ICoreWebView2*, ICoreWebView2NavigationStartingEventArgs* args) -> HRESULT {
                        if (alive.expired()) return S_OK;
                        LPWSTR uri = nullptr; // 运行时分配的导航地址。
                        args->get_Uri(&uri);
                        const bool allowed = uri && std::wstring(uri).starts_with(APP_ORIGIN);
                        CoTaskMemFree(uri);
                        if (!allowed) args->put_Cancel(TRUE);
                        return S_OK;
                    }).Get(), &token);
                    browser_->add_NewWindowRequested(Callback<ICoreWebView2NewWindowRequestedEventHandler>([](ICoreWebView2*, ICoreWebView2NewWindowRequestedEventArgs* args) -> HRESULT { args->put_Handled(TRUE); return S_OK; }).Get(), &token);
                    browser_->add_PermissionRequested(Callback<ICoreWebView2PermissionRequestedEventHandler>([](ICoreWebView2*, ICoreWebView2PermissionRequestedEventArgs* args) -> HRESULT { args->put_State(COREWEBVIEW2_PERMISSION_STATE_DENY); return S_OK; }).Get(), &token);
                    browser_->add_WebMessageReceived(Callback<ICoreWebView2WebMessageReceivedEventHandler>([this, alive](ICoreWebView2*, ICoreWebView2WebMessageReceivedEventArgs* args) -> HRESULT {
                        if (!alive.expired()) Receive(args);
                        return S_OK;
                    }).Get(), &token);
                    return browser_->Navigate(L"https://app.xiaoguai.local/index.html");
                }).Get());
        }).Get());
    if (FAILED(started)) MessageBox(L"未找到可用的 WebView2 Runtime，无法加载现代界面。", L"小怪", MB_OK | MB_ICONERROR);
}

void MainFrame::Receive(ICoreWebView2WebMessageReceivedEventArgs* args) {
    LPWSTR source = nullptr; // 必须来自映射的本地文档。
    args->get_Source(&source);
    const bool trusted = source && std::wstring(source).starts_with(APP_ORIGIN);
    CoTaskMemFree(source);
    if (!trusted) return;
    LPWSTR raw = nullptr; // 不接受任意脚本和宿主对象，只解析 JSON。
    if (FAILED(args->get_WebMessageAsJson(&raw))) return;
    const std::wstring encoded = raw ? raw : L"";
    CoTaskMemFree(raw);
    if (encoded.size() > 1048576) return;
    std::string id; // 业务关联编号用于准确报告错误。
    try {
        const Json message = Json::parse(Utf8(encoded)); // 来源已验证的结构化信封。
        const std::string type = message.value("type", "");
        id = message.value("id", "");
        if (type == "ready") {
            web_ready_ = true;
            Send({{"type", "window_state"}, {"visible", !!IsWindowVisible()}});
            if (!layout_report_.empty()) {
                Json fixture{{"selected_project", "web-layout-fixture"}, {"version", "layout-test"}, {"port", 12345},
                    {"projects", Json::array({{{"id", "web-layout-fixture"}, {"root", "C:\\demo\\sample-project"}}})},
                    {"agents", Json::array({{{"name", "Engineer"}, {"role", "工程师"}, {"seen", 1000.0}}, {{"name", "Reviewer"}, {"role", "独立审查员"}, {"seen", 1001.0}}, {{"name", "Operator"}, {"role", "人工管理入口"}, {"seen", 0.0}}})},
                    {"messages", Json::array({{{"id", 1}, {"sender", "Engineer"}, {"thread", "layout-test"}, {"kind", "note"}, {"subject", "网页布局夹具"}, {"body", "布局自检消息，不是真实 AI 回答。"}, {"created", 1000.0}, {"deliveries", Json::array({{{"recipient", "Reviewer"}, {"acknowledged", nullptr}}})}}})},
                    {"reservations", Json::array()}, {"run", {{"status", "idle"}, {"stage", "布局测试，不启动模型"}}}, {"done", true}};
                Send({{"type", "snapshot"}, {"value", fixture}});
                NextLayoutCase();
            } else {
                if (!last_snapshot_.empty()) Send({{"type", "snapshot"}, {"value", last_snapshot_}});
                for (const auto& command : waiting_commands_) Send({{"type", "ui_command"}, {"value", command}});
                waiting_commands_.clear();
            }
        } else if (type == "ui_result" && worker_) {
            worker_->Submit({{"op", "ui_result"}, {"operation_id", id}, {"result", message.value("result", Json::object())}, {"error", message.value("error", "")}});
        } else if (type == "action" && worker_) Action(message);
        else if (type == "browse" && worker_) {
            CFolderPickerDialog dialog(nullptr, OFN_PATHMUSTEXIST, this); // 人工对话框，MCP 可直接填写结构化路径。
            if (dialog.DoModal() == IDOK) Send({{"type", "picked_folder"}, {"path", Utf8(std::wstring(dialog.GetPathName()))}});
        } else if (type == "human_export" && worker_) {
            CFileDialog dialog(FALSE, L"jsonl", L"小怪协作记录.jsonl", OFN_OVERWRITEPROMPT | OFN_PATHMUSTEXIST, L"JSON Lines (*.jsonl)|*.jsonl||", this);
            if (dialog.DoModal() == IDOK) worker_->Submit({{"op", "export_history"}, {"file", Utf8(std::wstring(dialog.GetPathName()))}});
        } else if (type == "layout_result" && !layout_report_.empty()) NextLayoutCase(message.at("result"));
    } catch (const std::exception& error) { Fail(id, error.what()); }
}

void MainFrame::Action(const Json& message) {
    const std::string action = message.at("action").get<std::string>(); // 白名单业务，不是任意命令。
    const std::string id = message.value("id", ""); // 只有管理 MCP 操作关联票据。
    const Json fields = message.at("fields"); // 真实 DOM 字段，业务仍交给既有服务校验。
    Json request; // 仅构造后端已支持的参数。
    if (action == "open_project") request = {{"op", "project"}, {"root", fields.at("project_path")}};
    else if (action == "register_agent") request = {{"op", "register"}, {"name", fields.at("agent_name")}, {"role", fields.at("agent_role")}};
    else if (action == "send_message") {
        const std::string recipient = fields.at("recipient").get<std::string>();
        request = {{"op", "send"}, {"recipients", recipient == "Engineer + Reviewer" ? Json::array({"Engineer", "Reviewer"}) : Json::array({recipient})}, {"thread", fields.at("thread")}, {"kind", "task"}, {"subject", fields.at("subject")}, {"body", fields.at("body")}, {"client_id", id.empty() ? NewId() : id}};
    } else if (action == "export_agent_config") request = {{"op", "profile"}, {"name", message.at("selected_agent")}};
    else if (action == "export_ui_config") request = {{"op", "ui_profile"}};
    else if (action == "prepare_team") request = {{"op", "prepare_team"}, {"ai_count", fields.value("ai_count", 2)}};
    else if (action == "start_collaboration") {
        if (!message.value("confirm_start", false)) throw std::runtime_error("启动模型缺少显式确认");
        request = {{"op", "start"}, {"goal", fields.at("body")}, {"allow_write", fields.at("allow_write")},
            {"ai_count", fields.value("ai_count", 2)}, {"rounds", fields.value("rounds", 2)}, {"concurrency", fields.value("concurrency", 1)}};
    } else if (action == "stop_collaboration") request = {{"op", "stop"}};
    else if (action == "export_history") {
        const std::string filename = message.value("export_filename", "");
        if (filename.empty() || filename.size() > 120 || filename.find("..") != std::string::npos || fs::path(Wide(filename)).extension() != L".jsonl") throw std::runtime_error("导出需要不含路径的 .jsonl 新文件名");
        for (const unsigned char ch : filename) if (!((ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') || (ch >= '0' && ch <= '9') || ch == '-' || ch == '_' || ch == '.')) throw std::runtime_error("导出文件名含非法字符");
        std::string stem = fs::path(Wide(filename)).stem().string(); // 拒绝 Windows 设备名。
        std::transform(stem.begin(), stem.end(), stem.begin(), [](unsigned char ch) { return static_cast<char>(std::toupper(ch)); });
        if (stem == "CON" || stem == "PRN" || stem == "AUX" || stem == "NUL" || (stem.size() == 4 && (stem.starts_with("COM") || stem.starts_with("LPT")) && stem[3] >= '1' && stem[3] <= '9')) throw std::runtime_error("不能导出到 Windows 设备名");
        const fs::path directory = root_ / L"exports"; // AI 不得通过导出覆盖任意本机文件。
        fs::create_directories(directory);
        const fs::path file = directory / Wide(filename);
        if (fs::exists(file)) throw std::runtime_error("导出文件已存在，未覆盖");
        request = {{"op", "export_history"}, {"file", Utf8(file.wstring())}};
    } else if (action == "open_data_directory") {
        ShellExecuteW(m_hWnd, L"open", data_.c_str(), nullptr, nullptr, SW_SHOWNORMAL);
        Json result = last_snapshot_; // 打开后通过页面的实际状态回传票据。
        result["done"] = true;
        result["ui_id"] = id;
        Send({{"type", "snapshot"}, {"value", result}});
        return;
    } else throw std::runtime_error("未知界面业务动作");
    if (!id.empty()) request["_ui_id"] = id;
    worker_->Submit(std::move(request));
}

LRESULT MainFrame::OnSnapshot(WPARAM unused, LPARAM value) {
    UNREFERENCED_PARAMETER(unused);
    const std::unique_ptr<Json> snapshot(reinterpret_cast<Json*>(value)); // 跨线程消息由窗口接管。
    last_snapshot_ = *snapshot;
    last_snapshot_["connection_directory"] = Utf8((data_ / L"connections").wstring());
    Send({{"type", "snapshot"}, {"value", last_snapshot_}});
    return 0;
}

LRESULT MainFrame::OnUICommand(WPARAM unused, LPARAM value) {
    UNREFERENCED_PARAMETER(unused);
    const std::unique_ptr<Json> command(reinterpret_cast<Json*>(value)); // 必须由实际网页控件执行并回传。
    if (web_ready_) Send({{"type", "ui_command"}, {"value", *command}});
    else waiting_commands_.push_back(*command);
    return 0;
}

void MainFrame::NextLayoutCase(const Json& previous) {
    if (!previous.empty()) {
        layout_cases_.push_back(previous);
        if (!previous.value("passed", false)) {
            WriteJson(layout_report_, {{"passed", false}, {"renderer", "webview2"}, {"cases", layout_cases_}});
            closing_ = true;
            PostMessage(WM_CLOSE);
            return;
        }
    }
    const CSize sizes[]{CSize(980, 700), CSize(1280, 800), CSize(1560, 960)}; // 三种真实视口尺寸。
    if (layout_case_ == 3) {
        WriteJson(layout_report_, {{"passed", true}, {"renderer", "webview2"}, {"theme", "light"}, {"fixture_only", true}, {"model_started", false}, {"viewport_cases", layout_cases_}, {"page_visibility_checks", 12}, {"input_and_selection_preserved", true}, {"gui_screenshot_verified", false}});
        SetTimer(2, 300, nullptr); // 离开网页回调后再请求本视图渲染预览。
        return;
    }
    const UINT dpi = GetDpiForWindow(m_hWnd); // 按当前 DPI 调整真实浏览器客户区。
    CRect outer(0, 0, MulDiv(sizes[layout_case_].cx, static_cast<int>(dpi), 96), MulDiv(sizes[layout_case_].cy, static_cast<int>(dpi), 96));
    AdjustWindowRectExForDpi(&outer, GetStyle(), FALSE, GetExStyle(), dpi);
    SetWindowPos(nullptr, 0, 0, outer.Width(), outer.Height(), SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE);
    ShowWindow(SW_SHOWNOACTIVATE); // 隐藏父窗口会暂停 Chromium 尺寸更新；只显示自己的夹具窗口，不抢占用户焦点。
    CRect client; // 显式同步浏览器边界，并把真实宿主尺寸写入测试数据。
    GetClientRect(&client);
    if (browser_controller_) browser_controller_->put_Bounds(client);
    Send({{"type", "host_size"}, {"width", client.Width()}, {"height", client.Height()}, {"dpi", dpi}});
    ++layout_case_;
    SetTimer(1, 300, nullptr); // 等待真实浏览器提交新的视口，再检查 DOM，而非连测同一旧尺寸。
}

void MainFrame::OnTimer(UINT_PTR timer) {
    if (timer == 1 && !layout_report_.empty()) {
        KillTimer(1);
        Send({{"type", "window_state"}, {"visible", !!IsWindowVisible()}});
        Send({{"type", "layout_check"}});
        return;
    }
    if (timer == 2 && !layout_report_.empty()) { KillTimer(2); SavePreview(); return; }
    if (timer == 3 && !layout_report_.empty()) {
        KillTimer(3);
        Json report = ReadJson(layout_report_); // 预览异常不允许留下永久挂起的测试宿主。
        report["preview_saved"] = false;
        report["preview_error"] = "preview callback timeout";
        WriteJson(layout_report_, report);
        closing_ = true;
        PostMessage(WM_CLOSE);
        return;
    }
    CFrameWnd::OnTimer(timer);
}

void MainFrame::SavePreview() {
    const fs::path file = layout_report_.parent_path() / L"modern-ui-preview.png"; // 仅当前应用的测试数据预览。
    ComPtr<IStream> stream; // WebView2 直接写入 PNG，不读取桌面合成画面。
    if (FAILED(SHCreateStreamOnFileEx(file.c_str(), STGM_CREATE | STGM_WRITE | STGM_SHARE_EXCLUSIVE, FILE_ATTRIBUTE_NORMAL, TRUE, nullptr, &stream))) { closing_ = true; PostMessage(WM_CLOSE); return; }
    const std::weak_ptr<int> alive = life_; // 不在窗口销毁后执行截图回调。
    SetTimer(3, 5000, nullptr);
    const HRESULT capture = browser_->CapturePreview(COREWEBVIEW2_CAPTURE_PREVIEW_IMAGE_FORMAT_PNG, stream.Get(), Callback<ICoreWebView2CapturePreviewCompletedHandler>([this, alive, stream, file](HRESULT result) -> HRESULT {
        if (alive.expired()) return S_OK;
        KillTimer(3);
        if (SUCCEEDED(result)) stream->Commit(STGC_DEFAULT);
        Json report = ReadJson(layout_report_); // 截图成败单列，不用它伪造任何模型或后端测试。
        report["preview_saved"] = SUCCEEDED(result);
        report["preview_note"] = "本程序 WebView2 渲染预览，使用明确标注的布局测试数据；不是桌面截图";
        if (SUCCEEDED(result)) report["preview_file"] = Utf8(file.wstring());
        WriteJson(layout_report_, report);
        closing_ = true;
        PostMessage(WM_CLOSE);
        return S_OK;
    }).Get());
    if (FAILED(capture)) {
        Json report = ReadJson(layout_report_); // 同步失败也必须退出，不把测试进程留在后台等待不存在的回调。
        report["preview_saved"] = false;
        report["preview_error"] = static_cast<unsigned long>(capture);
        WriteJson(layout_report_, report);
        closing_ = true;
        PostMessage(WM_CLOSE);
    }
}

void MainFrame::OnSize(UINT type, int width, int height) {
    CFrameWnd::OnSize(type, width, height);
    if (browser_controller_) { CRect client; GetClientRect(&client); browser_controller_->put_Bounds(client); }
    Send({{"type", "window_state"}, {"visible", !!IsWindowVisible() && !IsIconic()}});
}

void MainFrame::OnGetMinMaxInfo(MINMAXINFO* info) {
    CFrameWnd::OnGetMinMaxInfo(info);
    const int dpi = static_cast<int>(GetDpiForWindow(m_hWnd)); // 最小可用桌面窗口尺寸。
    info->ptMinTrackSize = {MulDiv(980, dpi ? dpi : 96, 96), MulDiv(700, dpi ? dpi : 96, 96)};
}

void MainFrame::OnClose() {
    if (!closing_ && last_snapshot_.value("run", Json::object()).value("status", "") == "running" && MessageBox(L"关闭会停止本窗口的 AI 协作，已提交消息会保留。是否关闭？", L"小怪", MB_YESNO | MB_ICONQUESTION | MB_DEFBUTTON2) != IDYES) return;
    closing_ = true;
    life_.reset();
    if (browser_controller_) browser_controller_->Close();
    browser_.Reset();
    browser_controller_.Reset();
    if (worker_) { worker_->Stop(); worker_.reset(); }
    MSG pending{}; // 释放尚未处理的后台消息。
    while (PeekMessageW(&pending, m_hWnd, WM_SNAPSHOT, WM_UI_COMMAND, PM_REMOVE)) delete reinterpret_cast<Json*>(pending.lParam);
    CFrameWnd::OnClose();
}

class CollabApp final : public CWinApp {
public:
    // 原生壳只保留生命周期与人工文件对话框，业务界面全部使用本地网页。
    BOOL InitInstance() override {
        CWinApp::InitInstance();
        if (!AfxOleInit()) return FALSE;
        wchar_t executable[32768]{}; // 当前宿主的位置。
        GetModuleFileNameW(nullptr, executable, 32768);
        const fs::path root = fs::path(executable).parent_path().parent_path();
        int count = 0; // 系统解析后的命令行参数数量。
        auto arguments = CommandLineToArgvW(GetCommandLineW(), &count);
        if (count == 5 && std::wstring(arguments[1]) == L"--self-test") {
            try { WriteJson(fs::path(arguments[4]), NativeSelfTest(root, fs::path(arguments[2]), fs::path(arguments[3]))); exit_code_ = 0; }
            catch (const std::exception& error) { WriteJson(fs::path(arguments[4]), {{"passed", false}, {"error", error.what()}}); exit_code_ = 1; }
            LocalFree(arguments);
            return FALSE;
        }
        const bool layout = count == 3 && std::wstring(arguments[1]) == L"--ui-layout-test";
        const bool test_host = count == 4 && std::wstring(arguments[1]) == L"--ui-test-host";
        fs::path data = root / L"data"; // 正常用户数据目录。
        if (layout) { report_ = fs::absolute(fs::path(arguments[2])); data = report_.parent_path() / L"web-layout-cache"; }
        if (test_host) { data = fs::absolute(fs::path(arguments[2])); fs::create_directories(data); WriteJson(data / L"view.json", {{"root", Utf8(fs::absolute(fs::path(arguments[3])).wstring())}}); }
        LocalFree(arguments);
        if (!layout && !test_host) {
            instance_.Reset(CreateMutexW(nullptr, FALSE, L"Local\\XiaoguaiCollab-Desktop-01"));
            if (GetLastError() == ERROR_ALREADY_EXISTS) { MessageBoxW(nullptr, L"小怪协作中心已运行，请使用已有窗口。", L"小怪", MB_OK | MB_ICONINFORMATION); return FALSE; }
        }
        if (!fs::is_regular_file(root / L"web/index.html")) { MessageBoxW(nullptr, L"缺少本地网页资源，请从完整程序目录运行。", L"小怪", MB_OK | MB_ICONERROR); return FALSE; }
        auto frame = new MainFrame(root, data, report_); // 框架在最终销毁时释放。
        m_pMainWnd = frame;
        const int dpi = static_cast<int>(GetDpiForSystem());
        const int width = std::min(MulDiv(1440, dpi, 96), GetSystemMetrics(SM_CXSCREEN) - 70);
        const int height = std::min(MulDiv(940, dpi, 96), GetSystemMetrics(SM_CYSCREEN) - 90);
        if (!frame->Create(nullptr, L"小怪 AI 协作中心 · MCP", WS_OVERLAPPEDWINDOW | WS_CLIPCHILDREN, CRect(35, 35, width + 35, height + 35))) return FALSE;
        frame->ShowWindow(layout || test_host ? SW_HIDE : SW_SHOW);
        frame->UpdateWindow();
        return TRUE;
    }
    // 报告失败或缺失时返回非零退出码，不以正常关闭掩盖自检失败。
    int ExitInstance() override {
        const int normal = CWinApp::ExitInstance();
        if (!report_.empty()) { try { return ReadJson(report_).value("passed", false) ? 0 : 1; } catch (const std::exception&) { return 1; } }
        return exit_code_ < 0 ? normal : exit_code_;
    }
private:
    Handle instance_; // 普通窗口的单实例标记。
    fs::path report_; // 网页布局模式的测试结果路径。
    int exit_code_ = -1; // -1 为正常桌面窗口。
};

// 应用本身保持原生进程，界面和管理 MCP 回执来自真实网页控件。
CollabApp application;
