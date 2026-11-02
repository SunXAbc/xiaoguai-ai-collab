#include "Infrastructure.hpp"
#include <winhttp.h>
#include <wincrypt.h>
#include <algorithm>
#include <chrono>
#include <ctime>
#include <fstream>
#include <memory>
#include <stdexcept>
#include <thread>
#include <vector>

#pragma comment(lib, "winhttp.lib")
#pragma comment(lib, "crypt32.lib")

// 以固定说明保留 Win32 错误码，不把密钥、请求头或正文拼入异常。
static std::runtime_error WinError(const char* operation) {
    return std::runtime_error(std::string(operation) + "，Win32 错误码 " + std::to_string(GetLastError()));
}

std::wstring Wide(const std::string& value) {
    if (value.empty()) return {};
    const int size = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()), nullptr, 0); // 目标 Unicode 长度。
    if (!size) throw WinError("UTF-8 解码失败");
    std::wstring result(static_cast<size_t>(size), L'\0'); // 输出缓冲区。
    MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()), result.data(), size);
    return result;
}

std::string Utf8(const std::wstring& value) {
    if (value.empty()) return {};
    const int size = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()), nullptr, 0, nullptr, nullptr); // UTF-8 字节数。
    if (!size) throw WinError("Unicode 编码失败");
    std::string result(static_cast<size_t>(size), '\0'); // 输出缓冲区。
    WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()), result.data(), size, nullptr, nullptr);
    return result;
}

std::wstring Quote(const std::wstring& value) {
    std::wstring result = L"\""; // 首个引号。
    size_t slashes = 0; // 待转义的连续反斜杠数。
    for (const wchar_t ch : value) { // 当前输入字符。
        if (ch == L'\\') { ++slashes; continue; }
        if (ch == L'"') result.append(slashes * 2 + 1, L'\\');
        else result.append(slashes, L'\\');
        slashes = 0;
        result.push_back(ch);
    }
    result.append(slashes * 2, L'\\');
    result.push_back(L'"');
    return result;
}

std::string NewId() {
    GUID id{}; // 新业务标识。
    if (FAILED(CoCreateGuid(&id))) throw std::runtime_error("无法生成业务标识");
    wchar_t buffer[40]{}; // GUID 的固定长度 Unicode 表示。
    StringFromGUID2(id, buffer, 40);
    return Utf8(buffer);
}

std::string ClockText(double value) {
    if (value <= 0) return "尚未活动";
    const auto seconds = static_cast<time_t>(value) + 8 * 3600; // 固定按客户端要求的 UTC+08:00 显示。
    tm local{}; // 北京时间日历字段。
    gmtime_s(&local, &seconds);
    char buffer[40]{}; // 格式化时间结果。
    strftime(buffer, sizeof(buffer), "%m-%d %H:%M:%S", &local);
    return buffer;
}

Json ReadJson(const fs::path& file) {
    std::ifstream stream(file, std::ios::binary); // 支持 Unicode 文件名的输入流。
    if (!stream) throw std::runtime_error("无法读取状态文件");
    if (fs::file_size(file) > 8 * 1024 * 1024) throw std::runtime_error("状态文件过大");
    return Json::parse(stream);
}

void WriteJson(const fs::path& file, const Json& value) {
    const fs::path temporary = file.wstring() + L".pending"; // 同目录原子替换文件。
    {
        std::ofstream stream(temporary, std::ios::binary | std::ios::trunc); // 写入完整的新状态。
        if (!stream || !(stream << value.dump(2))) throw std::runtime_error("无法保存状态文件");
        stream.flush();
        if (!stream) throw std::runtime_error("写入状态失败");
    }
    if (!MoveFileExW(temporary.c_str(), file.c_str(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) throw WinError("原子保存状态失败");
}

// 从 file 读取 Windows DPAPI JSON；仅当前用户可解密，不退回明文。
static Json ProtectedJson(const fs::path& file) {
    std::ifstream stream(file, std::ios::binary); // 当前用户的加密身份文件。
    if (!stream || fs::file_size(file) > 1024 * 1024) throw std::runtime_error("无法读取加密身份文件");
    std::vector<BYTE> bytes((std::istreambuf_iterator<char>(stream)), {}); // 文件中的密文字节。
    DATA_BLOB input{static_cast<DWORD>(bytes.size()), bytes.data()}; // DPAPI 密文输入。
    DATA_BLOB output{}; // 系统分配的明文输出，需要清零并释放。
    if (!CryptUnprotectData(&input, nullptr, nullptr, nullptr, nullptr, CRYPTPROTECT_UI_FORBIDDEN, &output)) throw WinError("当前用户无法解密连接身份");
    try {
        Json value = Json::parse(output.pbData, output.pbData + output.cbData); // 解析后仅在受控内存中使用。
        SecureZeroMemory(output.pbData, output.cbData);
        LocalFree(output.pbData);
        return value;
    } catch (...) {
        SecureZeroMemory(output.pbData, output.cbData);
        LocalFree(output.pbData);
        throw;
    }
}

// WinHTTP 句柄专用析构器，避免把 HINTERNET 错用 CloseHandle。
struct InternetClose { void operator()(void* value) const { if (value) WinHttpCloseHandle(value); } };
// 每次请求拥有独立句柄，线程间不共享 WinHTTP 状态。
using Internet = std::unique_ptr<void, InternetClose>;

Json Http(const Endpoint& endpoint, const std::wstring& path, const Json* body, bool mcp) {
    Internet session(WinHttpOpen(L"XiaoguaiCollab/0.1.0", WINHTTP_ACCESS_TYPE_NO_PROXY, nullptr, nullptr, 0)); // 本机请求禁止系统代理。
    if (!session) throw WinError("创建 HTTP 会话失败");
    WinHttpSetTimeouts(session.get(), 2000, 2000, 5000, mcp ? 20000 : 5000);
    Internet connection(WinHttpConnect(session.get(), L"127.0.0.1", endpoint.port, 0)); // 只连接回环地址。
    if (!connection) throw WinError("连接本机服务失败");
    Internet request(WinHttpOpenRequest(connection.get(), body ? L"POST" : L"GET", path.c_str(), nullptr, nullptr, nullptr, 0)); // 固定请求方法，不接受任意外部地址。
    if (!request) throw WinError("创建本机请求失败");
    DWORD redirect = WINHTTP_OPTION_REDIRECT_POLICY_NEVER; // 凭据绝不跟随 HTTP 重定向。
    if (!WinHttpSetOption(request.get(), WINHTTP_OPTION_REDIRECT_POLICY, &redirect, sizeof(redirect))) throw WinError("关闭重定向失败");
    const std::wstring headers = L"Content-Type: application/json\r\nAccept: application/json, text/event-stream\r\nAuthorization: Bearer " + Wide(endpoint.credential) + (mcp ? L"\r\nMCP-Protocol-Version: 2025-11-25\r\n" : L"\r\n"); // 请求头只在当前调用内存在。
    std::string encoded = body ? body->dump() : ""; // UTF-8 请求正文。
    if (!WinHttpSendRequest(request.get(), headers.c_str(), static_cast<DWORD>(headers.size()), encoded.empty() ? nullptr : encoded.data(), static_cast<DWORD>(encoded.size()), static_cast<DWORD>(encoded.size()), 0) || !WinHttpReceiveResponse(request.get(), nullptr)) throw WinError("本机请求未完成；结果未知，请核对记录后操作");
    DWORD status = 0; // HTTP 状态码。
    DWORD status_size = sizeof(status); // 查询缓冲区大小。
    if (!WinHttpQueryHeaders(request.get(), WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER, nullptr, &status, &status_size, nullptr)) throw WinError("读取响应状态失败");
    std::string result; // 有界响应缓冲区。
    for (;;) {
        char buffer[16384]{}; // 每次读取的固定大小分片。
        DWORD read = 0; // 当前分片字节数。
        if (!WinHttpReadData(request.get(), buffer, sizeof(buffer), &read)) throw WinError("读取响应中断；请核对投递结果");
        if (!read) break;
        result.append(buffer, read);
        if (result.size() > 8 * 1024 * 1024) throw std::runtime_error("本机响应超过大小上限");
    }
    if (status < 200 || status >= 300) {
        // 管理错误仅提取明确的 error 字段，不回显任意完整响应。
        const Json error = Json::parse(result, nullptr, false); // 可能不是 JSON 的错误响应。
        throw std::runtime_error("本机服务 HTTP " + std::to_string(status) + (error.is_object() && error.contains("error") && error["error"].is_string() ? "：" + error["error"].get<std::string>() : ""));
    }
    return result.empty() ? Json::object() : Json::parse(result);
}

BackendHost::~BackendHost() { Stop(); }

void BackendHost::Start(const fs::path& root, const fs::path& data, const std::atomic_bool& cancel) {
    fs::create_directories(data);
    const fs::path executable = root / L"bin/collab-service/collab-service.exe"; // 独立打包后端，不依赖用户 Python。
    if (!fs::is_regular_file(executable)) throw std::runtime_error("缺少打包后端，请先运行构建脚本");
    job_.Reset(CreateJobObjectW(nullptr, nullptr));
    if (!job_.Get()) throw WinError("创建进程生命周期边界失败");
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{}; // 窗口退出后回收自有整棵进程树。
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if (!SetInformationJobObject(job_.Get(), JobObjectExtendedLimitInformation, &limits, sizeof(limits))) throw WinError("设置进程回收边界失败");
    SECURITY_ATTRIBUTES security{sizeof(SECURITY_ATTRIBUTES), nullptr, TRUE}; // 仅允许显式指定的日志和空输入句柄继承。
    Handle log(CreateFileW((data / "service.log").c_str(), FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE, &security, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr)); // 不含认证信息的错误日志。
    Handle input(CreateFileW(L"NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, &security, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr)); // 后端不接收终端输入。
    if (log.Get() == INVALID_HANDLE_VALUE || input.Get() == INVALID_HANDLE_VALUE) throw WinError("创建后端日志失败");
    SIZE_T attribute_size = 0; // 扩展启动属性所需大小。
    InitializeProcThreadAttributeList(nullptr, 1, 0, &attribute_size);
    std::vector<BYTE> attributes(attribute_size); // 仅在创建进程期间存在的属性缓冲区。
    STARTUPINFOEXW startup{}; // 限制子进程继承句柄。
    startup.StartupInfo.cb = sizeof(startup);
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    startup.StartupInfo.hStdInput = input.Get();
    startup.StartupInfo.hStdOutput = log.Get();
    startup.StartupInfo.hStdError = log.Get();
    startup.lpAttributeList = reinterpret_cast<LPPROC_THREAD_ATTRIBUTE_LIST>(attributes.data());
    if (!InitializeProcThreadAttributeList(startup.lpAttributeList, 1, 0, &attribute_size)) throw WinError("初始化进程属性失败");
    HANDLE inherited[]{input.Get(), log.Get()}; // 唯一允许继承的两个句柄。
    if (!UpdateProcThreadAttribute(startup.lpAttributeList, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, inherited, sizeof(inherited), nullptr, nullptr)) {
        DeleteProcThreadAttributeList(startup.lpAttributeList);
        throw WinError("设置句柄白名单失败");
    }
    const std::string instance = NewId(); // 与状态文件匹配的本次启动随机标识。
    std::wstring command = Quote(executable.wstring()) + L" serve --data " + Quote(data.wstring()) + L" --instance " + Quote(Wide(instance)); // 参数正确转义，不经过任何命令解释器。
    PROCESS_INFORMATION process{}; // 新进程的系统句柄。
    const BOOL created = CreateProcessW(executable.c_str(), command.data(), nullptr, nullptr, TRUE, CREATE_NO_WINDOW | CREATE_SUSPENDED | EXTENDED_STARTUPINFO_PRESENT, nullptr, root.c_str(), &startup.StartupInfo, &process); // 先挂起，加入 Job 后才执行代码。
    DeleteProcThreadAttributeList(startup.lpAttributeList);
    if (!created) throw WinError("启动本机后端失败");
    process_.Reset(process.hProcess);
    Handle thread(process.hThread); // 主线程句柄只在启动阶段使用。
    if (!AssignProcessToJobObject(job_.Get(), process_.Get())) {
        TerminateProcess(process_.Get(), 1);
        throw WinError("无法接管后端生命周期，未运行未受控后端");
    }
    if (ResumeThread(thread.Get()) == static_cast<DWORD>(-1)) throw WinError("恢复后端进程失败");
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(25); // 有界启动等待。
    while (!cancel && std::chrono::steady_clock::now() < deadline) {
        if (WaitForSingleObject(process_.Get(), 0) == WAIT_OBJECT_0) throw std::runtime_error("后端已退出，请检查 data/service.log");
        try {
            const Json state = ReadJson(data / "state.json"); // 旧状态文件不能冒充本次实例。
            if (state.value("instance", "") == instance) {
                Endpoint candidate{state.at("port").get<unsigned short>(), ProtectedJson(data / "admin.dpapi").at("key").get<std::string>()}; // 当前用户解密的管理连接。
                if (Http(candidate, L"/health").value("status", "") == "ok") {
                    endpoint_ = std::move(candidate);
                    return;
                }
            }
        } catch (const std::exception&) {
            // 文件尚未发布时只继续观察同一活进程，绝不重启或猜测结果。
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    throw std::runtime_error(cancel ? "启动已取消" : "本机服务启动超时，未自动重试");
}

void BackendHost::Stop() {
    if (!process_.Get()) return;
    if (WaitForSingleObject(process_.Get(), 0) == WAIT_TIMEOUT && endpoint_.port) {
        try {
            const Json body{{"op", "shutdown"}}; // 请求后端正常提交并停止自有工作线程。
            Http(endpoint_, L"/api/action", &body);
        } catch (const std::exception&) {}
        WaitForSingleObject(process_.Get(), 3500);
    }
    // 关闭 Job 同时回收尚未退出的后端和它派生的桥接/AI 进程。
    job_.Reset();
    WaitForSingleObject(process_.Get(), 2000);
    process_.Reset();
    endpoint_ = {};
}

// 用 endpoint 调用标准 MCP method/params；工具错误必须使原生验收失败。
static Json Rpc(const Endpoint& endpoint, const std::string& method, const Json& parameters) {
    const Json body{{"jsonrpc", "2.0"}, {"id", NewId()}, {"method", method}, {"params", parameters}}; // 每次请求有独立 RPC ID。
    const Json response = Http(endpoint, L"/mcp", &body, true); // 标准 JSON-RPC，非专有通信协议。
    if (response.contains("error")) throw std::runtime_error("原生 MCP 请求返回协议错误");
    return response.at("result");
}

Json NativeSelfTest(const fs::path& root, const fs::path& data, const fs::path& project) {
    std::atomic_bool cancel{false}; // 原生自检不需要交互取消。
    BackendHost backend; // 自检结束后自动释放服务和所有子进程。
    backend.Start(root, data, cancel);
    const Json open{{"op", "project"}, {"root", Utf8(project.wstring())}}; // 自检专用临时项目。
    const auto id = Http(backend.Admin(), L"/api/action", &open).at("id").get<std::string>(); // 真实项目标识。
    Endpoint agents[2]; // 两个独立认证的 MCP 客户端连接。
    for (int index = 0; index < 2; ++index) { // 分别导出、解密两个真实身份。
        const std::string name = index == 0 ? "Engineer" : "Reviewer"; // 当前测试角色。
        const Json export_request{{"op", "profile"}, {"project_id", id}, {"name", name}}; // 管理端配置请求。
        const Json configuration = Http(backend.Admin(), L"/api/action", &export_request); // 不含明文令牌。
        const fs::path profile = Wide(configuration.at("directory").get<std::string>()) + L"/" + Wide(configuration.at("prefix").get<std::string>()) + L".dpapi"; // 用户绑定密文。
        agents[index] = {backend.Admin().port, ProtectedJson(profile).at("token").get<std::string>()};
        const Json initialized = Rpc(agents[index], "initialize", {{"protocolVersion", "2025-11-25"}, {"capabilities", Json::object()}, {"clientInfo", {{"name", "native-selftest"}, {"version", "1"}}}}); // 实际旧版握手。
        if (initialized.at("protocolVersion") != "2025-11-25") throw std::runtime_error("MCP 旧版握手不符合预期");
        if (Rpc(agents[index], "tools/list", Json::object()).at("tools").size() != 7) throw std::runtime_error("MCP 工具发现失败");
    }
    const Json send{{"name", "collab_send"}, {"arguments", {{"recipients", {"Reviewer"}}, {"thread", "native-selftest"}, {"kind", "note"}, {"subject", "原生真实协议验收"}, {"body", "原生 C++ 双客户端中文消息；这不是 AI 生成的对话"}, {"client_id", NewId()}}}}; // 明确标注验收消息来源。
    const Json sent = Rpc(agents[0], "tools/call", send); // 工程师发送。
    const Json received = Rpc(agents[1], "tools/call", {{"name", "collab_inbox"}, {"arguments", Json::object()}}); // 审查员接收。
    if (sent.value("isError", false) || received.value("isError", false) || received.at("structuredContent").at("messages").at(0).at("sender") != "Engineer") throw std::runtime_error("原生双客户端真实投递失败");
    return {{"passed", true}, {"transport", "MCP JSON-RPC over loopback HTTP"}, {"clients", 2}, {"protocol", "2025-11-25"}, {"utf8", true}, {"note", "此项验证通信，不冒充模型推理"}};
}
