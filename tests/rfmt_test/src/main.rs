#![allow(dead_code)]
use std::collections::HashMap;
use std::fmt;
use std::rc::Rc;
use std::sync::{Arc, Mutex};

#[derive(Debug, Clone)]
struct Point {
    x: i32,
    y: i32,
}

#[derive(Debug)]
enum Shape {
    Circle { center: Point, radius: f64 },
    Line(Point, Point),
    Empty,
}

struct DisplayOnly(u32);
impl fmt::Display for DisplayOnly {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "display-only {}", self.0)
    }
}

struct Both(u32);
impl fmt::Display for Both {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "DISPLAY {}", self.0)
    }
}
impl fmt::Debug for Both {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "DEBUG {}", self.0)
    }
}

struct NoDebug(u32);

#[derive(Debug)]
struct Wrapper<T> {
    inner: T,
    tags: Vec<&'static str>,
}

#[derive(Debug)]
struct Failing;
impl fmt::Display for Failing {
    fn fmt(&self, _f: &mut fmt::Formatter<'_>) -> fmt::Result {
        Err(fmt::Error)
    }
}

struct ErrDebug;
impl fmt::Debug for ErrDebug {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("partial")?;
        Err(fmt::Error)
    }
}

#[inline(never)]
fn stop_here(_p: &Point, _s: &str, _n: u64) {
    println!("stop");
}

fn worker(m: Arc<Mutex<Vec<u32>>>) {
    loop {
        let mut g = m.lock().unwrap();
        g.push(1);
        if g.len() > 1_000_000 { g.clear(); }
        drop(g);
        std::thread::yield_now();
    }
}

fn main() {
    let p = Point { x: 1, y: -2 };
    let p_ref = &p;
    let p_box = Box::new(Point { x: 3, y: 4 });
    let p_raw: *const Point = &p;
    let shape = Shape::Circle { center: p.clone(), radius: 2.5 };
    let line = Shape::Line(Point { x: 0, y: 0 }, Point { x: 9, y: 9 });
    let empty = Shape::Empty;
    let disp = DisplayOnly(7);
    let both = Both(8);
    let nodebug = NoDebug(9);
    let wrapped = Wrapper { inner: Some(p.clone()), tags: vec!["a", "b"] };
    let mut map = HashMap::new();
    map.insert("one", 1u8);
    let rc = Rc::new(vec![1.5f32, 2.5]);
    let tuple = (1u8, "two", 3.0f64);
    let arr = [10u16, 20, 30];
    let slice_only = [7i64, 8, 9];   // only formatted through a slice below
    let ch = 'λ';
    let num = 42i32;
    let big = u128::MAX;
    let unit = ();
    let res: Result<u32, String> = Err("bad".to_string());
    let failing = Failing;
    let err_debug = ErrDebug;
    let text = String::from("héllo");
    let shared = Arc::new(Mutex::new(Vec::<u32>::new()));
    let m2 = shared.clone();
    std::thread::spawn(move || worker(m2));

    // instantiate the Debug impls we want available
    let _ = format!("{:?} {:?} {:?} {:?} {:?} {:?}", p, p_ref, p_box, p_raw, shape, line);
    let _ = format!("{:?} {} {:?} {} {:?} {:?}", empty, disp, both, both, wrapped, map);
    let _ = format!("{:?} {:?} {:?} {:?} {:?} {:?} {:?}", rc, tuple, arr, &slice_only[..], ch, num, big);
    let _ = format!("{:?} {:?} {:?} {:?}", unit, res, text, shared);
    let mut sink = String::new();
    let _ = std::fmt::Write::write_fmt(&mut sink, format_args!("{:?} {:?}", failing, err_debug));
    let _ = nodebug.0;

    stop_here(&p, "arg-str", 12345);
    println!("{}", disp);
}
