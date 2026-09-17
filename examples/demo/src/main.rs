use std::collections::HashMap;

#[derive(Debug, Default)]
struct Config {
    name: String,
    retries: u32,
    tags: Vec<&'static str>,
}

#[derive(Debug)]
enum State {
    Idle,
    Running { pid: u32, cfg: Config },
}

// 从未被 {:?} 打印过的类型：靠下面的 keep_debug_impls 保住它的 Debug::fmt
#[derive(Debug)]
struct NeverPrinted {
    scores: HashMap<String, f64>,
}

/// 让 rustc 为这些类型生成并保留 Debug::fmt，供调试器调用。
/// 只取函数指针，不需要构造值，也不会真的打印任何东西。
#[cfg(debug_assertions)]
fn keep_debug_impls() {
    use std::fmt::{Debug, Formatter, Result};
    macro_rules! keep { ($($t:ty),*) => { $( std::hint::black_box(<$t as Debug>::fmt as fn(&$t, &mut Formatter) -> Result); )* } }
    keep!(NeverPrinted, HashMap<String, f64>, Option<Config>);
}

fn main() {
    #[cfg(debug_assertions)]
    keep_debug_impls();

    let cfg = Config { name: "svc".into(), retries: 3, tags: vec!["a", "b"] };
    let state = State::Running { pid: 42, cfg: Config::default() };
    let mut never = NeverPrinted { scores: HashMap::new() };
    never.scores.insert("x".into(), 1.5);
    let maybe: Option<Config> = None;

    println!("{:?} {:?}", cfg, state);     // 这两个类型被正常使用过
    println!("{}", never.scores.len());    // NeverPrinted 本身从未被 {:?} 过
    let _ = &maybe;
}
