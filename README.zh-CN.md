# rust-debug-fmt

[![CI](https://github.com/hsqStephenZhang/rust-debug-fmt/actions/workflows/ci.yml/badge.svg)](https://github.com/hsqStephenZhang/rust-debug-fmt/actions/workflows/ci.yml)

在 **gdb** 和 **lldb** 里把 Rust 变量打印成和 `{:?}` / `{:#?}` 一模一样的样子，方法是直接调用被调试进程里自己的 `core::fmt::Debug::fmt`。

```
(gdb) rlocals                      (lldb) rlocals
config = Config { name: "svc", retries: 3, tags: ["a", "b"] }
state  = Running { pid: 42, cfg: Config { name: "", retries: 0, tags: [] } }
scores = {"x": 1.5}
maybe  = None
```

调试器自带的 Rust 打印只会把结构体原样展开（`alloc::string::String {vec: alloc::vec::Vec<u8, alloc::alloc::Global> {buf: ...`），没有 Rust 插件的 lldb 显示得更少。这个扩展换了一条路：在二进制里找到 rustc 已经编好的单态化函数 `<T as Debug>::fmt`，在被调试进程内部以一个 `String` 为输出目标调用它，把文本读回来再释放。因此输出永远和程序自己打印的完全一致：自定义 `Debug`、`HashMap`、`Option`、枚举、多层泛型、第三方库类型，全部原样呈现。

思路来自 [BugStalker](https://github.com/godzie44/BugStalker) 的 `vard` / `argd` 命令，这里是把它移植到 gdb 和 lldb 的 Python API 上，Rust 相关的知识两边共用。

## 一键上手

一行命令，之后这台机器上的每个 `gdb` / `lldb` 会话都自带这些命令：

```sh
curl -fsSL https://raw.githubusercontent.com/hsqStephenZhang/rust-debug-fmt/main/install.sh | sh
```

安装脚本会把仓库放到 `~/.rust-debug-fmt`，检测你装了 gdb 还是 lldb，并在 `~/.gdbinit` / `~/.lldbinit` 里追加一段带标记的配置。再跑一次就是更新；`sh ~/.rust-debug-fmt/install.sh --uninstall` 会完整卸载。然后在任何 Rust 项目里：

```sh
cargo build
gdb target/debug/your-bin          # 或者：lldb target/debug/your-bin
(gdb) break your_crate::main
(gdb) run
(gdb) rlocals                      # 所有局部变量，按 {:?} 输出
(gdb) rprint some_var.field        # 单个表达式
```

项目本身不需要做任何改动。如果某个类型被报告缺少 `Debug::fmt`，见[让 `Debug::fmt` 存在于二进制里](#让-debugfmt-存在于二进制里)。

<details>
<summary>手动安装</summary>

纯 Python，除调试器自带的解释器外没有任何依赖。

```sh
git clone https://github.com/hsqStephenZhang/rust-debug-fmt ~/rust-debug-fmt

# gdb（Linux）
echo 'source ~/rust-debug-fmt/rust_debug_fmt_gdb.py' >> ~/.gdbinit

# lldb（Linux、macOS）
echo 'command script import ~/rust-debug-fmt/rust_debug_fmt_lldb.py' >> ~/.lldbinit
```

`gdb` / `rust-gdb` 和 `lldb` / `rust-lldb` 都会读取这些 init 文件。只想在某次会话里临时加载，就在调试器里执行那一行 `source` / `command script import`。
</details>

| | 要求 | 实测 |
|---|---|---|
| gdb | gdb ≥ 13，带 Python 3 | gdb 15（Ubuntu 24.04）、gdb 17 |
| lldb | lldb ≥ 14，带 Python 3 | lldb 18（Ubuntu）、22（Arch）、macOS arm64 的 Xcode lldb |
| rustc | ≥ 1.81，带 debuginfo（默认的 `cargo build` 即可） | 1.84、1.86、1.89、stable、nightly；legacy 与 v0 两种 mangling |
| 平台 | Linux x86-64、macOS arm64 | 两者都在 CI 里 |

**macOS：** 现代 macOS 上 gdb 基本不可用（不支持 Apple Silicon、需要签名），请用 lldb 后端。macOS 的 CI 任务跑的就是它。

## 命令

| 命令 | gdb | lldb | 作用 |
|---|---|---|---|
| `rprint EXPR [EXPR ...]` | ✓ | ✓ | 对每个表达式输出 `{:?}` |
| `rprint/p EXPR`、`rprint -p EXPR` | ✓ | 只有 `-p` | `{:#?}`，多行缩进；lldb 把 `/x` 语法留给了自己 |
| `rlocals [-p]` | ✓ | ✓ | 当前帧全部局部变量（BugStalker 的 `vard locals`） |
| `rargs [-p]` | ✓ | ✓ | 当前帧全部参数（`argd all`） |
| `$rfmt(EXPR [, 1])` | ✓ | – | 返回文本的便捷函数，用于 `printf`、`dprintf`、断点条件 |
| `set rfmt-auto on` | ✓ | `rfmt-set auto on` | 自动模式：`print` / `frame variable` / 看板走 Debug::fmt（见下文） |
| `set rfmt-verbose on` | ✓ | `rfmt-set verbose on` | 打印选中的符号和调用过程 |
| `set rfmt-scheduler-lock off` | ✓ | `rfmt-set scheduler-lock off` | 调用期间允许其他线程运行（默认只跑当前线程） |
| | | `rfmt-set timeout 60` | 表达式超时秒数（lldb，默认 30） |

表达式用调试器自己的语法：gdb 里是 Rust 语法；lldb 里是变量路径（`a`、`a.b`、`*p`、`arr[2]`）或 C 表达式。某个变量拿不到 `Debug::fmt` 时，`rlocals` / `rargs` 会回退到调试器原生打印，并说明原因。

```
(gdb) rprint cfg.tags map[0] *boxed
(gdb) printf "state = %s\n", $rfmt(state)
(gdb) dprintf worker.rs:88, "job = %s\n", $rfmt(job)

(lldb) rprint -p state
(lldb) rargs
```

## 自动模式：`print`、`frame variable`、看板、IDE

默认关闭。打开后，调试器**自己的**命令对每个有 `Debug::fmt` 的聚合类型都显示 Debug 输出；标量、指针和没有 `Debug` 的类型保持原生显示：

```
(gdb) set rfmt-auto on                 (lldb) rfmt-set auto on
(gdb) print state                      (lldb) v state
$1 = Running { pid: 42, cfg: Config { name: "", retries: 0, tags: [] } }
(gdb) info locals                      (lldb) frame variable
```

覆盖范围包括 gdb-dashboard 的 Variables / Expressions 面板、`display`、`finish` 的 "Value returned"、lldb 的 `p` / `v` / `frame variable`，以及建立在它们之上的 IDE 变量视图（VS Code 的 CodeLLDB 或原生 lldb 适配器）。gdb 里是一个排在 rust-gdb 之前的 pretty printer；lldb 里是 `rust-debug-fmt` 分类下的 type summary。把那行 `set` 写进 init 文件就是常开。

| 设置 | gdb | lldb |
|---|---|---|
| 开启 | `set rfmt-auto on` | `rfmt-set auto on` |
| 用 `{:#?}` 代替 `{:?}` | `set rfmt-auto-pretty on`（`auto` 则跟随 `set print pretty`） | `rfmt-set pretty on` |

代价要清楚：每显示一个值就会在你的进程里跑一次 `Debug::fmt`，一个每次 `step` 都刷新全部局部变量的看板，每个变量就是一次 inferior call。重入保护保证我们自己发起的调用不会再触发 printer，失败的情况一律回退到原生显示。

### VS Code + CodeLLDB

CodeLLDB 自带 lldb 和 Python，不需要额外安装。在 `launch.json` 里加两项（想对所有会话生效，就写进 `settings.json` 的 `lldb.launch.initCommands` / `lldb.launch.postRunCommands`）：

```jsonc
{
  "type": "lldb",
  "request": "launch",
  "name": "my-bin",
  "cargo": { "args": ["build", "--bin=my-bin"] },
  "sourceLanguages": ["rust"],
  "initCommands": ["command script import ~/.rust-debug-fmt/rust_debug_fmt_lldb.py"],
  "postRunCommands": ["rfmt-set auto on"]
}
```

之后 Variables 面板、Watch、悬停提示和 Debug Console（`rprint x`、`v x`）都是 Debug 输出。`rfmt-set auto on` 特意放在 `postRunCommands`：CodeLLDB 在创建 target 时加载 Rust 工具链的 formatter，而 lldb 让最近一次启用的 formatter 分类优先。`rfmt-set auto on` 还会装一个 stop-hook，每次停下都重新把我们的分类排到最前，所以顺序只影响第一次停下之前的显示。`examples/demo/.vscode/launch.json` 是完整示例。

## 让 `Debug::fmt` 存在于二进制里

rustc 只会单态化程序真正用到的东西，链接器还会丢掉没用到的 std 代码。所以只有程序在某处用 `{:?}` 格式化过 `T`，二进制里才有 `<T as Debug>::fmt`。否则会看到：

```
no `<demo::Thing as core::fmt::Debug>::fmt` in the binary; the program has to
format this type with {:?} somewhere for rustc to emit it
```

解决办法不需要真的打印任何东西：在一个只在 debug 构建里存在的钩子中取一次函数指针即可。只涉及类型，不需要构造值。

```rust
#[cfg(debug_assertions)]
fn keep_debug_impls() {
    use std::fmt::{Debug, Formatter, Result};
    macro_rules! keep {
        ($($t:ty),*) => { $( std::hint::black_box(
            <$t as Debug>::fmt as fn(&$t, &mut Formatter) -> Result); )* }
    }
    keep!(Thing, Vec<Thing>, Option<Config>, HashMap<String, f64>);
}

fn main() {
    #[cfg(debug_assertions)]
    keep_debug_impls();
    // ...
}
```

`examples/demo` 有一个完整示例。基本标量（整数、浮点、`bool`、`char`、`()`）不需要这样处理：它们的 `Debug` 实现通常不会被链接进来，扩展直接在本地格式化。

## 限制

- **被优化掉的值**打不出来。放在寄存器里的值会先拷到被调试进程里，可以正常打印。
- **裸指针打印的是指向的值。** 两个调试器都把 `&T`、`*const T`、`*mut T` 拼成同一种写法，脚本会优先选引用的实现；真正的 `{:?}` 对裸指针打印的是地址。
- **它会在你的进程里执行真实代码。** `Debug::fmt` 会经过全局分配器，也可能拿锁。如果你正好停在分配器内部，或者类型的 `Debug` 需要另一个（此时已暂停的）线程持有的锁，调用会死锁；中断它，调试器会回滚。这和 BugStalker 的 `vard`、`call` / `expr` 是同样的代价。
- **`Debug` 实现的副作用会真实发生。** 绝大多数实现是纯函数，但如果某个实现会写日志或改状态，它就会真的执行。
- **lldb 在消息里用 C 的方式拼类型名**（`demo::Config *`、`unsigned int`），因为它没有 Rust 语言插件。打印出来的值不受影响，那是你的程序自己生成的。
- 会话中第一条命令会索引一遍所有 `fmt` 函数。一个 100 MB、含约 8000 个 `fmt` 函数的 debug 二进制在 gdb 里大约 3 秒，之后有缓存。

## 原理一段话

列出所有名为 `fmt` 的函数及其 DWARF 签名，`T` 对应的那个形如 `(&T, &mut core::fmt::Formatter)`；还原后的 linkage name 用来区分 `Debug` 和 `Display`。然后在被调试进程的一块临时内存里（lldb 分配的内存，或 gdb 里栈指针之下的区域）写入 `String::new()` 的头部、一张手工拼装的 `<String as core::fmt::Write>` vtable 和一个 `core::fmt::Formatter`；`Formatter` 和 `String` 的字段偏移都从 DWARF 读取，因此同一条代码路径覆盖 rustc 1.81 到当前版本。接着由调试器发起 inferior call（开着崩溃回滚），读回 cap/ptr/len，解码字节，调用 `drop_in_place::<String>` 释放堆内存。细节以及路上踩到的调试器坑见 [docs/how-it-works.md](docs/how-it-works.md)。

## 目录结构

```
rust_debug_fmt_gdb.py        gdb 入口（source ...）
rust_debug_fmt_lldb.py       lldb 入口（command script import ...）
rustdebugfmt/core.py         与调试器无关：符号名、布局、vtable、候选选择、调用流程
rustdebugfmt/gdb_backend.py  gdb Backend + 命令
rustdebugfmt/lldb_backend.py lldb Backend + 命令
tests/                       回归程序、gdb/lldb 会话脚本、期望输出
examples/demo/               演示 keep_debug_impls 钩子的最小项目
```

## 测试

```sh
tests/run.sh                        # gdb，当前工具链
DEBUGGER=lldb tests/run.sh          # lldb
tests/run.sh +1.84                  # 任意 rustup 工具链
RUSTFLAGS="-C symbol-mangling-version=v0" tests/run.sh
PROFILE=opt1 tests/run.sh           # 优化构建；被优化掉的值对应的行预期会 MISSING
```

`tests/rfmt_test` 覆盖了结构体、泛型、枚举、`Option`、`Result`、`Vec`、`HashMap`、`Rc`、`Arc<Mutex<_>>`、元组、数组（直接以及通过 `[T]`）、`u128`、`char`、`&str`、`String`、`Box`、引用、裸指针、只实现 `Display` 的类型、没有 `Debug` 的类型、返回 `Err` 的 `Debug` 实现，以及调用期间有第二个线程在运行的情况。CI 在 Linux（gdb 和 lldb，stable / 1.84 / nightly，legacy 与 v0 mangling）和 macOS（lldb）上运行它。

## 致谢

方法来自 BugStalker（其 `src/debugger/call/fmt.rs`）；本项目把它重新实现在 gdb 和 lldb 的 inferior call 机制之上。
