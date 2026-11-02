#pragma once
#include <afxwin.h>
#include <atomic>
#include <filesystem>
#include <string>
#include "third_party/json.hpp"

// 共享 JSON 类型；只用于本机 API 和明文协作消息，不包含模型内部思考。
using Json = nlohmann::json;
// 项目内使用原生 Unicode 文件路径。
namespace fs = std::filesystem;

// 将 UTF-8 文本 value 转为 Windows Unicode，非法编码抛出异常。
std::wstring Wide(const std::string& value);
// 将 Windows Unicode 文本 value 转为 UTF-8。
std::string Utf8(const std::wstring& value);
// 为单个 Windows 命令行参数 value 正确处理引号和末尾反斜杠。
std::wstring Quote(const std::wstring& value);
// 创建不包含认证数据的随机业务标识。
std::string NewId();
// 以北京时间显示秒时间戳 value；0 表示尚无活动。
std::string ClockText(double value);
// 从 file 读取有大小限制的 JSON，失败时抛出异常。
Json ReadJson(const fs::path& file);
// 将 value 原子写入 file，不允许半个 JSON 文件被读取。
void WriteJson(const fs::path& file, const Json& value);

class Handle final {
public:
    // value 为本实例独占的 Win32 句柄，析构时关闭。
    explicit Handle(HANDLE value = nullptr) : value_(value) {}
    ~Handle() { Reset(); }
    Handle(const Handle&) = delete;
    Handle& operator=(const Handle&) = delete;
    // 取得不转移所有权的原始句柄。
    HANDLE Get() const { return value_; }
    // 关闭旧句柄并接管 value。
    void Reset(HANDLE value = nullptr) {
        if (value_ && value_ != INVALID_HANDLE_VALUE) CloseHandle(value_);
        value_ = value;
    }
private:
    HANDLE value_; // 当前独占的系统句柄。
};

struct Endpoint {
    unsigned short port = 0; // 操作系统实际分配的回环端口。
    std::string credential; // 只在内存中保存的连接令牌，禁止日志输出。
};

// 通过回环地址调用 path；body=nullptr 时 GET，否则 POST；所有请求关闭代理与重定向。
Json Http(const Endpoint& endpoint, const std::wstring& path, const Json* body = nullptr, bool mcp = false);

class BackendHost final {
public:
    ~BackendHost();
    // 启动 root 下的已打包服务，数据放入 data；cancel 可在启动等候时取消。
    void Start(const fs::path& root, const fs::path& data, const std::atomic_bool& cancel);
    // 正常请求关闭，超时只回收自己的 Job Object，不影响其他 AI 进程。
    void Stop();
    // 返回当前管理端点；调用者不得把令牌放入界面、配置示例或日志。
    const Endpoint& Admin() const { return endpoint_; }
private:
    Handle job_; // 包含本服务及其自有 AI 子进程的生命周期边界。
    Handle process_; // 自己启动的后端进程。
    Endpoint endpoint_; // 启动确认后才设置的管理连接。
};

// 对打包服务执行真实双 MCP 客户端自检；project 为测试项目，返回可保存的非敏感报告。
Json NativeSelfTest(const fs::path& root, const fs::path& data, const fs::path& project);
