//! Build script: generate the ignored app icons, then run Tauri's build hook.
//!
//! Icons are generated rather than committed because the repository's content
//! gate permits no binary files.  The generated `icons/` directory is ignored
//! by Git and removed before delivery; a clean checkout builds it on demand.

use std::fs;
use std::path::{Path, PathBuf};

fn main() {
    ensure_icons();
    tauri_build::build()
}

fn ensure_icons() {
    let dir = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").expect("manifest dir"))
        .join("icons");
    fs::create_dir_all(&dir).expect("create icons directory");
    // A formal build writes the owner-supplied artwork here first.  Keep those
    // files untouched; only a clean checkout without brand art gets a neutral
    // generated fallback so the project still compiles.
    let complete = ["32x32.png", "128x128.png", "128x128@2x.png", "icon.ico"]
        .iter()
        .all(|name| dir.join(name).is_file());
    if complete {
        return;
    }
    let sizes = [16u32, 24, 32, 48, 64, 128, 256];
    let mut pngs: Vec<(u32, Vec<u8>)> = Vec::new();
    for size in sizes {
        let png = png(size, size, |x, y| pixel(x, y, size));
        pngs.push((size, png));
    }
    for (size, bytes) in &pngs {
        let name = match size {
            32 => "32x32.png".to_string(),
            128 => "128x128.png".to_string(),
            256 => "128x128@2x.png".to_string(),
            other => format!("{other}x{other}.png"),
        };
        write_if_changed(&dir.join(name), bytes);
    }
    write_if_changed(&dir.join("icon.ico"), &ico(&pngs));
    let large = pngs
        .iter()
        .find(|(size, _)| *size == 256)
        .map(|(_, bytes)| bytes.as_slice())
        .expect("256px icon");
    write_if_changed(&dir.join("icon.png"), large);
}

/// Do not touch an unchanged icon: rewriting it makes Tauri's file watcher
/// rebuild the application in an endless loop during `tauri dev`.
fn write_if_changed(path: &Path, bytes: &[u8]) {
    if let Ok(existing) = fs::read(path) {
        if existing == bytes {
            return;
        }
    }
    fs::write(path, bytes).expect("write generated icon");
}

/// Compute one pixel of the QUINTESSENCE mark: a dark rounded tile, a teal
/// ring with a gap, and a short tail.  Alpha outside the tile is zero.
fn pixel(x: u32, y: u32, size: u32) -> [u8; 4] {
    let s = size as f32;
    let cx = x as f32 + 0.5;
    let cy = y as f32 + 0.5;
    let radius = s * 0.46;
    let dx = cx - s / 2.0;
    let dy = cy - s / 2.0;
    let distance = (dx * dx + dy * dy).sqrt();
    let tile = s * 0.44;
    let corner = s * 0.12;
    let qx = (cx - s / 2.0).abs() - (tile - corner);
    let qy = (cy - s / 2.0).abs() - (tile - corner);
    let outside = (qx.max(0.0).powi(2) + qy.max(0.0).powi(2)).sqrt()
        + qx.max(qy).min(0.0)
        - corner;
    if outside > 0.0 {
        return [0, 0, 0, 0];
    }
    let ring = (distance - radius * 0.62).abs() <= (s * 0.055).max(1.0);
    let gap = dx > 0.0 && dy > 0.0 && distance > radius * 0.45;
    let tail = dx > 0.0
        && dy > 0.0
        && (dx - dy).abs() < (s * 0.07).max(1.0)
        && distance < radius * 0.86
        && distance > radius * 0.42;
    if (ring && !gap) || tail {
        return [53, 160, 140, 255];
    }
    [13, 28, 32, 255]
}

fn png(width: u32, height: u32, mut color: impl FnMut(u32, u32) -> [u8; 4]) -> Vec<u8> {
    let mut raw: Vec<u8> = Vec::with_capacity((width * height * 4 + height) as usize);
    for y in 0..height {
        raw.push(0);
        for x in 0..width {
            let [r, g, b, a] = color(x, y);
            raw.extend_from_slice(&[r, g, b, a]);
        }
    }
    let mut out = Vec::new();
    out.extend_from_slice(&[0x89, b'P', b'N', b'G', 0x0d, 0x0a, 0x1a, 0x0a]);
    let mut ihdr = Vec::new();
    ihdr.extend_from_slice(&width.to_be_bytes());
    ihdr.extend_from_slice(&height.to_be_bytes());
    ihdr.extend_from_slice(&[8, 6, 0, 0, 0]);
    chunk(&mut out, b"IHDR", &ihdr);
    chunk(&mut out, b"IDAT", &zlib(&raw));
    chunk(&mut out, b"IEND", &[]);
    out
}

fn chunk(out: &mut Vec<u8>, kind: &[u8; 4], data: &[u8]) {
    out.extend_from_slice(&(data.len() as u32).to_be_bytes());
    out.extend_from_slice(kind);
    out.extend_from_slice(data);
    let mut crc_input = Vec::with_capacity(4 + data.len());
    crc_input.extend_from_slice(kind);
    crc_input.extend_from_slice(data);
    out.extend_from_slice(&crc32(&crc_input).to_be_bytes());
}

fn zlib(data: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    out.extend_from_slice(&[0x78, 0x01]);
    let mut offset = 0;
    if data.is_empty() {
        out.extend_from_slice(&[0x01, 0x00, 0x00, 0xff, 0xff]);
    }
    while offset < data.len() {
        let remaining = data.len() - offset;
        let take = remaining.min(65535);
        let final_block = offset + take == data.len();
        out.push(if final_block { 1 } else { 0 });
        out.extend_from_slice(&(take as u16).to_le_bytes());
        out.extend_from_slice(&(!(take as u16)).to_le_bytes());
        out.extend_from_slice(&data[offset..offset + take]);
        offset += take;
    }
    out.extend_from_slice(&adler32(data).to_be_bytes());
    out
}

fn crc32(data: &[u8]) -> u32 {
    let mut crc = 0xffff_ffffu32;
    for byte in data {
        crc ^= u32::from(*byte);
        for _ in 0..8 {
            if crc & 1 != 0 {
                crc = (crc >> 1) ^ 0xedb8_8320;
            } else {
                crc >>= 1;
            }
        }
    }
    !crc
}

fn adler32(data: &[u8]) -> u32 {
    let mut a = 1u32;
    let mut b = 0u32;
    for byte in data {
        a = (a + u32::from(*byte)) % 65521;
        b = (b + a) % 65521;
    }
    (b << 16) | a
}

fn ico(images: &[(u32, Vec<u8>)]) -> Vec<u8> {
    let count = images.len() as u16;
    let mut header = Vec::new();
    header.extend_from_slice(&0u16.to_le_bytes());
    header.extend_from_slice(&1u16.to_le_bytes());
    header.extend_from_slice(&count.to_le_bytes());
    let mut entries = Vec::new();
    let mut offset = 6 + 16 * images.len() as u32;
    for (size, png) in images {
        let dimension = if *size >= 256 { 0 } else { *size as u8 };
        entries.push(dimension);
        entries.push(dimension);
        entries.push(0);
        entries.push(0);
        entries.extend_from_slice(&1u16.to_le_bytes());
        entries.extend_from_slice(&32u16.to_le_bytes());
        entries.extend_from_slice(&(png.len() as u32).to_le_bytes());
        entries.extend_from_slice(&offset.to_le_bytes());
        offset += png.len() as u32;
    }
    let mut out = header;
    out.extend_from_slice(&entries);
    for (_, png) in images {
        out.extend_from_slice(png);
    }
    out
}

#[allow(dead_code)]
fn exists(path: &Path) -> bool {
    path.exists()
}
