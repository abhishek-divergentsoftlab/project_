/**
 * International System of Units (SI) & Standard Trade Units Definition.
 *
 * Provides scientifically verified SI conversion factors, major global trade currencies,
 * and real-time total order calculations for B2B RFQs.
 */

export interface MajorCurrency {
  code: string;
  symbol: string;
  name: string;
}

export const MAJOR_CURRENCIES: MajorCurrency[] = [
  { code: "INR", symbol: "₹", name: "Indian Rupee (INR - ₹)" },
  { code: "USD", symbol: "$", name: "US Dollar (USD - $)" },
  { code: "EUR", symbol: "€", name: "Euro (EUR - €)" },
  { code: "GBP", symbol: "£", name: "British Pound (GBP - £)" },
  { code: "AED", symbol: "AED", name: "UAE Dirham (AED)" },
  { code: "SAR", symbol: "SAR", name: "Saudi Riyal (SAR)" },
  { code: "JPY", symbol: "¥", name: "Japanese Yen (JPY - ¥)" },
  { code: "SGD", symbol: "S$", name: "Singapore Dollar (SGD - S$)" },
  { code: "CAD", symbol: "C$", name: "Canadian Dollar (CAD - C$)" },
  { code: "AUD", symbol: "A$", name: "Australian Dollar (AUD - A$)" },
  { code: "CNY", symbol: "¥", name: "Chinese Yuan (CNY - ¥)" },
];

export type UnitCategory = "mass" | "volume" | "length" | "count" | "packaging";

export interface SiStandardUnit {
  value: string;
  label: string;
  symbol: string;
  category: UnitCategory;
  categoryLabel: string;
  baseUnit: string;
  multiplierToBase: number; // how many base units make 1 of this unit
  siFormula: string;
}

export const SI_UNITS: SiStandardUnit[] = [
  // --- Mass / Weight (SI base: kg) ---
  {
    value: "tonnes",
    label: "Metric Tonne (t / MT)",
    symbol: "t",
    category: "mass",
    categoryLabel: "Mass / Weight (SI)",
    baseUnit: "kg",
    multiplierToBase: 1000,
    siFormula: "1 tonne (t) = 1,000 kg = 1,000,000 g",
  },
  {
    value: "quintal",
    label: "Quintal (q)",
    symbol: "q",
    category: "mass",
    categoryLabel: "Mass / Weight (SI)",
    baseUnit: "kg",
    multiplierToBase: 100,
    siFormula: "1 quintal (q) = 100 kg = 100,000 g",
  },
  {
    value: "kg",
    label: "Kilogram (kg)",
    symbol: "kg",
    category: "mass",
    categoryLabel: "Mass / Weight (SI)",
    baseUnit: "kg",
    multiplierToBase: 1,
    siFormula: "SI base unit of mass · 1 kg = 1,000 g",
  },
  {
    value: "g",
    label: "Gram (g)",
    symbol: "g",
    category: "mass",
    categoryLabel: "Mass / Weight (SI)",
    baseUnit: "kg",
    multiplierToBase: 0.001,
    siFormula: "1 g = 0.001 kg = 1,000 mg",
  },

  // --- Volume / Liquids (SI Metric base: litres) ---
  {
    value: "litres",
    label: "Litre (L)",
    symbol: "L",
    category: "volume",
    categoryLabel: "Volume / Liquids (SI/Metric)",
    baseUnit: "litres",
    multiplierToBase: 1,
    siFormula: "SI metric unit of volume · 1 L = 1,000 mL = 0.001 m³",
  },
  {
    value: "ml",
    label: "Millilitre (mL)",
    symbol: "mL",
    category: "volume",
    categoryLabel: "Volume / Liquids (SI/Metric)",
    baseUnit: "litres",
    multiplierToBase: 0.001,
    siFormula: "1 mL = 0.001 L · 1,000 mL = 1 L",
  },
  {
    value: "kl",
    label: "Kilolitre (kL)",
    symbol: "kL",
    category: "volume",
    categoryLabel: "Volume / Liquids (SI/Metric)",
    baseUnit: "litres",
    multiplierToBase: 1000,
    siFormula: "1 kL = 1,000 L = 1 m³",
  },
  {
    value: "m3",
    label: "Cubic Metre (m³)",
    symbol: "m³",
    category: "volume",
    categoryLabel: "Volume / Liquids (SI/Metric)",
    baseUnit: "litres",
    multiplierToBase: 1000,
    siFormula: "SI derived unit of volume · 1 m³ = 1,000 L",
  },

  // --- Length / Distance (SI base: metres) ---
  {
    value: "metres",
    label: "Metre (m)",
    symbol: "m",
    category: "length",
    categoryLabel: "Length / Distance (SI)",
    baseUnit: "metres",
    multiplierToBase: 1,
    siFormula: "SI base unit of length · 1 m = 100 cm = 1,000 mm",
  },
  {
    value: "km",
    label: "Kilometre (km)",
    symbol: "km",
    category: "length",
    categoryLabel: "Length / Distance (SI)",
    baseUnit: "metres",
    multiplierToBase: 1000,
    siFormula: "1 km = 1,000 m",
  },
  {
    value: "cm",
    label: "Centimetre (cm)",
    symbol: "cm",
    category: "length",
    categoryLabel: "Length / Distance (SI)",
    baseUnit: "metres",
    multiplierToBase: 0.01,
    siFormula: "1 cm = 0.01 m · 100 cm = 1 m",
  },
  {
    value: "mm",
    label: "Millimetre (mm)",
    symbol: "mm",
    category: "length",
    categoryLabel: "Length / Distance (SI)",
    baseUnit: "metres",
    multiplierToBase: 0.001,
    siFormula: "1 mm = 0.001 m · 1,000 mm = 1 m",
  },

  // --- Count & Commercial Packaging ---
  {
    value: "pieces",
    label: "Pieces / Units (pcs)",
    symbol: "pcs",
    category: "count",
    categoryLabel: "Count & Commercial Packaging",
    baseUnit: "pieces",
    multiplierToBase: 1,
    siFormula: "Discrete unit count (1 unit = 1 piece)",
  },
  {
    value: "dozen",
    label: "Dozen (dz - 12 pcs)",
    symbol: "dz",
    category: "count",
    categoryLabel: "Count & Commercial Packaging",
    baseUnit: "pieces",
    multiplierToBase: 12,
    siFormula: "1 dozen = 12 pieces",
  },
  {
    value: "boxes",
    label: "Boxes (box)",
    symbol: "box",
    category: "packaging",
    categoryLabel: "Count & Commercial Packaging",
    baseUnit: "pieces",
    multiplierToBase: 1,
    siFormula: "Commercial box packaging",
  },
  {
    value: "cartons",
    label: "Cartons (ctn)",
    symbol: "ctn",
    category: "packaging",
    categoryLabel: "Count & Commercial Packaging",
    baseUnit: "pieces",
    multiplierToBase: 1,
    siFormula: "Corrugated master packaging carton",
  },
  {
    value: "bags",
    label: "Bags (e.g. 50 kg gunny / poly)",
    symbol: "bag",
    category: "packaging",
    categoryLabel: "Count & Commercial Packaging",
    baseUnit: "pieces",
    multiplierToBase: 1,
    siFormula: "B2B commodity trade bag / sack",
  },
  {
    value: "drums",
    label: "Drums / Barrels (200 L)",
    symbol: "drum",
    category: "packaging",
    categoryLabel: "Count & Commercial Packaging",
    baseUnit: "pieces",
    multiplierToBase: 1,
    siFormula: "Liquid/chemical bulk drum container",
  },
  {
    value: "pallets",
    label: "Pallets",
    symbol: "pallet",
    category: "packaging",
    categoryLabel: "Count & Commercial Packaging",
    baseUnit: "pieces",
    multiplierToBase: 1,
    siFormula: "Standard industrial cargo pallet",
  },
];

/** Lookup a unit definition by its normalized string */
export function findSiUnit(unitValue: string | undefined): SiStandardUnit | undefined {
  if (!unitValue) return undefined;
  const clean = unitValue.trim().toLowerCase().replace(/[.\s]/g, "");

  // Direct value match
  const direct = SI_UNITS.find((u) => u.value.toLowerCase() === clean);
  if (direct) return direct;

  // Synonyms and abbreviation matches
  const synMap: Record<string, string> = {
    t: "tonnes",
    ton: "tonnes",
    tonne: "tonnes",
    tons: "tonnes",
    mt: "tonnes",
    q: "quintal",
    qtl: "quintal",
    quintals: "quintal",
    k: "kg",
    "1k": "kg",
    "1kg": "kg",
    kg: "kg",
    kgs: "kg",
    kilo: "kg",
    kilos: "kg",
    kilogram: "kg",
    kilograms: "kg",
    g: "g",
    gm: "g",
    gms: "g",
    gram: "g",
    grams: "g",
    l: "litres",
    liter: "litres",
    liters: "litres",
    litre: "litres",
    litres: "litres",
    ml: "ml",
    milliliter: "ml",
    millilitre: "ml",
    kl: "kl",
    m3: "m3",
    m: "metres",
    meter: "metres",
    meters: "metres",
    metre: "metres",
    metres: "metres",
    km: "km",
    cm: "cm",
    mm: "mm",
    pc: "pieces",
    pcs: "pieces",
    piece: "pieces",
    pieces: "pieces",
    unit: "pieces",
    units: "pieces",
    dz: "dozen",
    doz: "dozen",
    dozen: "dozen",
    box: "boxes",
    boxes: "boxes",
    ctn: "cartons",
    carton: "cartons",
    cartons: "cartons",
    bag: "bags",
    bags: "bags",
    drum: "drums",
    drums: "drums",
    pallet: "pallets",
    pallets: "pallets",
  };

  const mapped = synMap[clean];
  if (mapped) {
    return SI_UNITS.find((u) => u.value === mapped);
  }

  return undefined;
}

export interface CalculationResult {
  isValid: boolean;
  totalAmount?: number;
  formattedTotal?: string;
  siSummary?: string;
  effectiveQuantityInPriceUnit?: number;
  conversionFormula?: string;
  isSameDimension: boolean;
  warning?: string;
}

/**
 * Calculates the exact SI total order estimate given quantity, price, and their respective units.
 */
export function calculateOrderEstimate(
  quantityVal: number | string,
  quantityUnitVal: string,
  priceVal: number | string,
  priceUnitVal: string,
  currencyCode: string = "INR"
): CalculationResult {
  const qty = Number(quantityVal);
  const price = Number(priceVal);

  if (Number.isNaN(qty) || qty <= 0 || Number.isNaN(price) || price <= 0) {
    return { isValid: false, isSameDimension: true };
  }

  const qUnit = findSiUnit(quantityUnitVal);
  const pUnit = findSiUnit(priceUnitVal);

  const currencyObj = MAJOR_CURRENCIES.find((c) => c.code === currencyCode) || {
    code: currencyCode,
    symbol: currencyCode === "INR" ? "₹" : currencyCode === "USD" ? "$" : currencyCode === "EUR" ? "€" : currencyCode,
  };

  function formatMoney(amount: number): string {
    if (currencyCode === "INR") {
      return `${currencyObj.symbol} ${amount.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
    }
    return `${currencyObj.symbol} ${amount.toLocaleString("en-US", { maximumFractionDigits: 2 })}`;
  }

  // Case 1: Identical units (e.g. 10 kg @ ₹70/kg or 10 tonnes @ ₹70,000/tonne)
  if (!pUnit || !qUnit || qUnit.value === pUnit.value) {
    const total = qty * price;
    const uLabel = qUnit?.symbol || quantityUnitVal;
    return {
      isValid: true,
      totalAmount: total,
      formattedTotal: formatMoney(total),
      siSummary: `${qty.toLocaleString()} ${uLabel} @ ${currencyObj.symbol}${price.toLocaleString()}/${uLabel}`,
      conversionFormula: qUnit?.siFormula,
      isSameDimension: true,
    };
  }

  // Case 2: Same physical dimension (e.g. Mass: 10 tonnes @ ₹70/kg)
  if (qUnit.category === pUnit.category) {
    // Effective multiplier from quantity unit to price unit
    // Example: tonnes (1000) to kg (1) => factor is 1000 / 1 = 1000
    const factor = qUnit.multiplierToBase / pUnit.multiplierToBase;
    const effectiveQtyInPriceUnit = qty * factor;
    const total = effectiveQtyInPriceUnit * price;

    const summary = `${qty.toLocaleString()} ${qUnit.symbol} = ${effectiveQtyInPriceUnit.toLocaleString()} ${pUnit.symbol} @ ${currencyObj.symbol}${price.toLocaleString()}/${pUnit.symbol}`;
    const formula = `SI: 1 ${qUnit.symbol} = ${factor.toLocaleString()} ${pUnit.symbol}`;

    return {
      isValid: true,
      totalAmount: total,
      formattedTotal: formatMoney(total),
      siSummary: summary,
      effectiveQuantityInPriceUnit: effectiveQtyInPriceUnit,
      conversionFormula: formula,
      isSameDimension: true,
    };
  }

  // Case 3: Different physical dimensions (e.g. Mass vs Volume)
  return {
    isValid: true,
    totalAmount: qty * price,
    formattedTotal: formatMoney(qty * price),
    siSummary: `${qty.toLocaleString()} ${qUnit.label} @ ${currencyObj.symbol}${price.toLocaleString()} per ${pUnit.label}`,
    isSameDimension: false,
    warning: `Quantity is in ${qUnit.categoryLabel} while price is per ${pUnit.categoryLabel}. Ensure conversion density is specified in notes.`,
  };
}
