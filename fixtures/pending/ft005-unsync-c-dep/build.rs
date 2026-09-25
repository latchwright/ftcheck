fn main() {
    cc::Build::new().file("src/legacy.c").compile("legacy");
    println!("cargo:rerun-if-changed=src/legacy.c");
}
