#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <napi.h>

#include <algorithm>
#include <filesystem>
#include <string>
#include <vector>

namespace fs = std::filesystem;

struct ScheduleFailure {
  std::wstring path;
  DWORD errorCode;
  std::wstring errorMessage;
};

static std::wstring AddLongPathPrefix(const std::wstring& input) {
  if (input.rfind(LR"(\\?\)", 0) == 0) {
    return input;
  }
  if (input.rfind(LR"(\\)", 0) == 0) {
    // UNC path: \\server\share\foo -> \\?\UNC\server\share\foo
    return L"\\\\?\\UNC\\" + input.substr(2);
  }
  return L"\\\\?\\" + input;
}

static std::wstring FormatWin32Error(DWORD code) {
  LPWSTR buffer = nullptr;
  DWORD flags = FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_IGNORE_INSERTS;
  DWORD len = FormatMessageW(
      flags,
      nullptr,
      code,
      MAKELANGID(LANG_NEUTRAL, SUBLANG_DEFAULT),
      reinterpret_cast<LPWSTR>(&buffer),
      0,
      nullptr);

  std::wstring out;
  if (len && buffer) {
    out.assign(buffer, buffer + len);
    while (!out.empty() && (out.back() == L'\r' || out.back() == L'\n' || out.back() == L' ' || out.back() == L'\t')) {
      out.pop_back();
    }
  } else {
    out = L"Unknown error";
  }

  if (buffer) {
    LocalFree(buffer);
  }
  return out;
}

static std::string WideToUtf8(const std::wstring& input) {
  if (input.empty()) return {};

  int required = WideCharToMultiByte(
      CP_UTF8,
      0,
      input.data(),
      static_cast<int>(input.size()),
      nullptr,
      0,
      nullptr,
      nullptr);

  if (required <= 0) return {};

  std::string out(static_cast<size_t>(required), '\0');
  WideCharToMultiByte(
      CP_UTF8,
      0,
      input.data(),
      static_cast<int>(input.size()),
      out.data(),
      required,
      nullptr,
      nullptr);
  return out;
}

static bool ScheduleDeleteOnRebootOne(const std::wstring& widePath, DWORD* outErr) {
  std::wstring prefixed = AddLongPathPrefix(widePath);
  if (MoveFileExW(prefixed.c_str(), nullptr, MOVEFILE_DELAY_UNTIL_REBOOT) != 0) {
    if (outErr) *outErr = ERROR_SUCCESS;
    return true;
  }
  if (outErr) *outErr = GetLastError();
  return false;
}

static size_t PathDepth(const fs::path& p) {
  return static_cast<size_t>(std::distance(p.begin(), p.end()));
}

static Napi::Value ScheduleDeleteOnReboot(const Napi::CallbackInfo& info) {
  Napi::Env env = info.Env();

  if (info.Length() < 1 || !info[0].IsString()) {
    Napi::TypeError::New(env, "path must be a string").ThrowAsJavaScriptException();
    return env.Null();
  }

  const std::string inputUtf8 = info[0].As<Napi::String>().Utf8Value();
  if (inputUtf8.empty()) {
    Napi::TypeError::New(env, "path must not be empty").ThrowAsJavaScriptException();
    return env.Null();
  }

  std::error_code ec;
  fs::path root = fs::path(inputUtf8);
  root = fs::absolute(root, ec);
  if (ec) {
    // absolute 失败也不致命，继续用原路径
    ec.clear();
    root = fs::path(inputUtf8);
  }

  if (!fs::exists(root, ec) || ec) {
    Napi::Object result = Napi::Object::New(env);
    result.Set("scheduled", Napi::Array::New(env));
    result.Set("failed", Napi::Array::New(env));
    return result;
  }

  std::vector<fs::path> files;
  std::vector<fs::path> dirs;

  fs::file_status rootStatus = fs::symlink_status(root, ec);
  if (ec) {
    ec.clear();
    rootStatus = fs::status(root, ec);
  }

  const bool rootIsDirectory = !ec && fs::is_directory(rootStatus) && !fs::is_symlink(rootStatus);

  if (rootIsDirectory) {
    fs::recursive_directory_iterator it(
        root,
        fs::directory_options::skip_permission_denied,
        ec);

    fs::recursive_directory_iterator end;
    for (; !ec && it != end; ++it) {
      const fs::path current = it->path();
      std::error_code se;
      fs::file_status st = it->symlink_status(se);
      if (se) {
        continue;
      }

      if (fs::is_directory(st) && !fs::is_symlink(st)) {
        dirs.push_back(current);
      } else {
        files.push_back(current);
      }
    }

    std::sort(dirs.begin(), dirs.end(), [](const fs::path& a, const fs::path& b) {
      const size_t da = PathDepth(a);
      const size_t db = PathDepth(b);
      if (da != db) return da > db; // 深的先删
      return a.native() > b.native();
    });
  } else {
    files.push_back(root);
  }

  Napi::Array scheduled = Napi::Array::New(env);
  Napi::Array failed = Napi::Array::New(env);
  uint32_t scheduledIndex = 0;
  uint32_t failedIndex = 0;

  auto pushScheduled = [&](const fs::path& p) {
    scheduled.Set(scheduledIndex++, Napi::String::New(env, WideToUtf8(p.native())));
  };

  auto pushFailure = [&](const fs::path& p, DWORD code) {
    Napi::Object item = Napi::Object::New(env);
    item.Set("path", Napi::String::New(env, WideToUtf8(p.native())));
    item.Set("errorCode", Napi::Number::New(env, static_cast<double>(code)));
    item.Set("errorMessage", Napi::String::New(env, WideToUtf8(FormatWin32Error(code))));
    failed.Set(failedIndex++, item);
  };

  for (const auto& p : files) {
    DWORD err = ERROR_SUCCESS;
    if (ScheduleDeleteOnRebootOne(p.native(), &err)) {
      pushScheduled(p);
    } else {
      pushFailure(p, err);
    }
  }

  for (const auto& p : dirs) {
    DWORD err = ERROR_SUCCESS;
    if (ScheduleDeleteOnRebootOne(p.native(), &err)) {
      pushScheduled(p);
    } else {
      pushFailure(p, err);
    }
  }

  if (rootIsDirectory) {
    DWORD err = ERROR_SUCCESS;
    if (ScheduleDeleteOnRebootOne(root.native(), &err)) {
      pushScheduled(root);
    } else {
      pushFailure(root, err);
    }
  }

  Napi::Object result = Napi::Object::New(env);
  result.Set("scheduled", scheduled);
  result.Set("failed", failed);
  return result;
}

Napi::Object Init(Napi::Env env, Napi::Object exports) {
  exports.Set("scheduleDeleteOnReboot", Napi::Function::New(env, ScheduleDeleteOnReboot));
  return exports;
}

NODE_API_MODULE(delayed_delete, Init)