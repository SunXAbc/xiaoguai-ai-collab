#pragma once
#include "Infrastructure.hpp"
#include <condition_variable>
#include <deque>
#include <fstream>
#include <memory>
#include <mutex>
#include <thread>

// 原生宿主的后台快照与管理 MCP 命令；仅投递本程序自己的消息。
constexpr UINT WM_SNAPSHOT = WM_APP + 10;
constexpr UINT WM_UI_COMMAND = WM_APP + 11;

class Controller final {
public:
    // window 是接收快照的真实窗口；root 是完整程序目录。
    Controller(HWND window, fs::path root, fs::path data = {}) : window_(window), root_(std::move(root)), data_(data.empty() ? root_ / L"data" : std::move(data)) {
        worker_ = std::thread([this] { Loop(); });
    }
    ~Controller() { Stop(); }
    // 将用户单次操作 value 放入队列；不在失败时自动重发。
    void Submit(Json value) {
        std::lock_guard<std::mutex> guard(mutex_); // 队列锁只保护入队，不包围网络请求。
        if (stop_) return;
        queue_.push_back(std::move(value));
        condition_.notify_one();
    }
    // 等待自有线程结束；窗口销毁前调用，保证不再向已销毁窗口投递消息。
    void Stop() {
        stop_ = true;
        condition_.notify_one();
        if (worker_.joinable()) worker_.join();
    }
private:
    // 向 UI 移交 value 的所有权；投递失败时自行释放。
    void Post(Json value, UINT message = WM_SNAPSHOT) {
        auto packet = std::make_unique<Json>(std::move(value)); // 窗口消息唯一拥有的数据包。
        if (PostMessageW(window_, message, 0, reinterpret_cast<LPARAM>(packet.get()))) packet.release();
    }
    // 在后台读取管理快照并补充当前选中的项目标识。
    void Snapshot(bool done = false, const std::string& notice = "", const std::string& ui_id = "") {
        Json state = Http(host_.Admin(), L"/api/state?project=" + Wide(project_)); // 当前项目快照，不阻塞 UI 线程。
        state["selected_project"] = project_;
        state["done"] = done;
        if (!notice.empty()) state["notice"] = notice;
        if (!ui_id.empty()) state["ui_id"] = ui_id;
        Post(std::move(state));
    }
    // 从已经鉴权的本机服务领取一个 UI 命令；只在 UI 线程执行，不在后台直接改控件。
    void PollUI() {
        const Json response = Http(host_.Admin(), L"/api/ui/next"); // 管理身份专用队列。
        if (response.contains("command") && response.at("command").is_object()) Post(response.at("command"), WM_UI_COMMAND);
    }
    // 打开 path 对应的项目，只登记协作身份，不修改该目录代码。
    void Open(const std::string& path) {
        const Json request{{"op", "project"}, {"root", path}}; // 人工明确选择的目录。
        project_ = Http(host_.Admin(), L"/api/action", &request).at("id").get<std::string>();
        WriteJson(data_ / L"view.json", {{"root", path}});
    }
    // 执行 value 中一次明确的管理操作，返回可展示的结果说明。
    std::string Execute(Json value) {
        const std::string operation = value.value("op", ""); // 固定白名单操作。
        if (operation == "ui_result") {
            Http(host_.Admin(), L"/api/ui/result", &value);
            return "";
        }
        if (operation == "ui_profile") {
            Http(host_.Admin(), L"/api/action", &value);
            return "界面控制 MCP 配置已导出到 data/connections/desktop-ui.mcp.json；仅交给可信管理 AI";
        }
        if (operation == "project") {
            Open(value.at("root").get<std::string>());
            return "项目已打开；不会自动启动模型";
        }
        if (project_.empty()) throw std::runtime_error("请先打开一个项目目录");
        if (operation == "export_history") {
            const fs::path file = Wide(value.at("file").get<std::string>()); // 用户在保存对话框中指定的文件。
            std::ofstream stream(file, std::ios::binary | std::ios::trunc); // 完整消息 JSONL，不包含认证配置。
            if (!stream) throw std::runtime_error("无法创建导出文件");
            long long after = 0; // 完整历史的分页游标，不受界面 300 条窗口限制。
            size_t count = 0; // 实际导出的消息总数。
            for (;;) {
                const Json page = Http(host_.Admin(), L"/api/state?project=" + Wide(project_) + L"&after=" + std::to_wstring(after)); // 下一批真实历史。
                if (page.at("messages").empty()) break;
                for (const auto& item : page.at("messages")) { // 逐条保存消息和确认记录。
                    stream << item.dump() << "\n";
                    after = item.at("id").get<long long>();
                    ++count;
                }
                if (!stream) throw std::runtime_error("导出写入失败，文件可能不完整");
            }
            stream.flush();
            if (!stream) throw std::runtime_error("导出写入失败，文件可能不完整");
            return "已导出 " + std::to_string(count) + " 条完整消息；不包含连接凭据";
        }
        value["project_id"] = project_;
        const Json result = Http(host_.Admin(), L"/api/action", &value); // 写操作只有一次发送，网络结果不明交由人工核对。
        if (operation == "profile") return "MCP 接入配置已导出到 data/connections；每个 AI 使用自己的配置片段，不要覆盖原有配置";
        if (operation == "send") return "人工消息已提交，编号 " + std::to_string(result.at("id").get<long long>());
        if (operation == "prepare_team") return "团队已准备：" + std::to_string(result.at("ai_count").get<int>()) + " 个 AI 身份；未调用模型，未删除其他成员";
        if (operation == "start") return "团队已启动；最多 " + std::to_string(result.at("planned_calls").get<int>()) + " 次调用，并发上限 " + std::to_string(result.at("concurrency").get<int>()) + "；界面只展示实际交换的协作结论";
        if (operation == "stop") return "已请求停止；执行器正在回收自己的 AI 子进程";
        return "操作已完成";
    }
    // 唯一后台线程负责启动、排队操作和只读快照；失败不会重复执行用户操作。
    void Loop() {
        try {
            host_.Start(root_, data_, stop_);
            const fs::path saved = data_ / L"view.json"; // 上次人工选择的项目目录。
            try {
                Open(fs::exists(saved) ? ReadJson(saved).at("root").get<std::string>() : Utf8((root_ / L"sample-project").wstring()));
            } catch (const std::exception& error) {
                Post({{"error", std::string("请重新选择项目：") + error.what()}, {"done", true}});
            }
            Snapshot(true, "本机 MCP 服务已就绪；未自动启动任何 AI");
            PollUI();
            while (!stop_) {
                Json command; // 本轮至多取出一项用户操作。
                {
                    std::unique_lock<std::mutex> guard(mutex_); // 等候期间释放锁，允许 UI 入队。
                    condition_.wait_for(guard, std::chrono::seconds(1), [this] { return stop_ || !queue_.empty(); });
                    if (stop_) break;
                    if (!queue_.empty()) { command = std::move(queue_.front()); queue_.pop_front(); }
                }
                const std::string ui_id = command.is_object() ? command.value("_ui_id", "") : ""; // 与异步 UI 业务操作关联的去重编号。
                if (command.is_object()) command.erase("_ui_id");
                try {
                    const std::string notice = command.is_null() ? "" : Execute(std::move(command)); // 不自动重试失败命令。
                    Snapshot(!notice.empty(), notice, ui_id);
                    PollUI();
                } catch (const std::exception& error) {
                    Post({{"error", error.what()}, {"done", true}, {"ui_id", ui_id}});
                }
            }
        } catch (const std::exception& error) {
            Post({{"error", std::string("服务启动失败：") + error.what()}, {"done", true}, {"fatal", true}});
        }
        host_.Stop();
    }
    HWND window_; // 不拥有窗口，只在 Stop 返回前使用。
    fs::path root_; // 程序根目录。
    fs::path data_; // 当前用户私有数据目录。
    BackendHost host_; // 当前窗口拥有的本机后端与子进程。
    std::string project_; // 仅后台线程写入的当前项目 ID。
    std::atomic_bool stop_{false}; // 窗口关闭的跨线程取消信号。
    std::mutex mutex_; // 管理命令队列的互斥锁。
    std::condition_variable condition_; // 避免后台忙等。
    std::deque<Json> queue_; // 用户明确提交的操作，不保存隐藏任务。
    std::thread worker_; // 单个网络与生命周期工作线程。
};
