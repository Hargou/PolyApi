/**
 * Test order book parsing - run with: node test_book_parse.js
 * Simulates the parseLevel logic from index.html
 */

function parseLevel(x) {
  var price, size;
  if (x && typeof x === 'object' && ('price' in x || 'size' in x)) {
    price = x.price;
    size = x.size;
  } else if (Array.isArray(x) && x.length >= 2) {
    price = x[0];
    size = x[1];
  } else return null;
  var p = parseFloat(price),
    s = parseFloat(size);
  if (p > 1 && s >= 0 && s <= 1) {
    var t = price;
    price = size;
    size = t;
  }
  return { price: price, size: size };
}

function displayLevel(level) {
  var p = parseFloat(level.price);
  var pct = (p >= 0 && p <= 1 ? p * 100 : p).toFixed(1) + "%";
  return pct + " | " + (level.size != null ? level.size : "-");
}

// Test cases from Polymarket docs (object format)
const objBids = [
  { price: "0.48", size: "1000" },
  { price: "0.47", size: "2500" },
];
const objAsks = [
  { price: "0.52", size: "800" },
  { price: "0.53", size: "1500" },
];

// Potential swapped format (if API sends size as price)
const swappedAsks = [
  { price: "222.8", size: "0.48" },
  { price: "123", size: "0.52" },
];

console.log("=== Object format (Polymarket standard) ===");
objBids.forEach((b, i) => {
  const parsed = parseLevel(b);
  console.log("  BID", i, ":", displayLevel(parsed));
});
objAsks.forEach((a, i) => {
  const parsed = parseLevel(a);
  console.log("  ASK", i, ":", displayLevel(parsed));
});

console.log("\n=== Swapped price/size (would show 222.8, 123) ===");
swappedAsks.forEach((a, i) => {
  const parsed = parseLevel(a);
  console.log("  ASK", i, ":", displayLevel(parsed), "  [after swap heuristic]");
});

console.log("\n=== Array format [price, size] ===");
const arrBids = [
  ["0.99", "995"],
  ["0.05", "100"],
];
arrBids.forEach((b, i) => {
  const parsed = parseLevel(b);
  console.log("  BID", i, ":", displayLevel(parsed));
});
