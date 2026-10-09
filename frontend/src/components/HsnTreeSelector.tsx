import { useState, useEffect, useRef, useCallback } from "react";
import {
  hsnApi,
  type HsnItem,
  type HsnCategoryGroup,
  type HsnChapterItem,
  type HsnSuggestion,
} from "@/api/endpoints";
import {
  IconSearch,
  IconChevronDown,
  IconChevronRight,
  IconCheck,
  IconClose,
  IconSparkles,
} from "@/components/icons";

export interface HsnSelection {
  hsnCode: string;
  normalizedCode: string;
  productName: string;
  category: string;
  fullName: string;
  path: string[];
}

interface HsnTreeSelectorProps {
  selectedHsnCode?: string;
  onSelect: (selection: HsnSelection) => void;
  role?: "buyer" | "seller";
}

// Fallback initial categories if API is loading
const FALLBACK_CATEGORIES: HsnCategoryGroup[] = [
  {
    category: "Agriculture",
    count: 24,
    total_items: 3328,
    chapters: [
      { chapter: "04", description: "Dairy produce; milk, butter, cheese, curd, natural honey", total_items: 180 },
      { chapter: "07", description: "Edible vegetables and certain roots and tubers (Pulses, Moong, Chana)", total_items: 450 },
      { chapter: "10", description: "Cereals (Rice, Wheat, Maize/Corn, Barley)", total_items: 280 },
      { chapter: "09", description: "Coffee, tea, mate and spices (Ginger, Turmeric, Cumin)", total_items: 320 },
      { chapter: "12", description: "Oil seeds and oleaginous fruits (Soybean, Mustard, Sunflower)", total_items: 260 },
      { chapter: "08", description: "Edible fruit and nuts (Almonds, Cashews, Walnuts)", total_items: 310 },
      { chapter: "01", description: "Live animals", total_items: 110 },
    ],
  },
  {
    category: "Industrial",
    count: 14,
    total_items: 7055,
    chapters: [
      { chapter: "72", description: "Iron and steel (TMT bars, Steel Coils, Billets)", total_items: 890 },
      { chapter: "73", description: "Articles of iron or steel (Pipes, Tubes, Fasteners)", total_items: 640 },
      { chapter: "84", description: "Nuclear reactors, boilers, machinery and mechanical appliances", total_items: 2450 },
      { chapter: "74", description: "Copper and articles thereof (Copper wire, Rods)", total_items: 280 },
    ],
  },
  {
    category: "Electronics",
    count: 1,
    total_items: 1275,
    chapters: [
      { chapter: "85", description: "Electrical machinery; Solar panels, Inverters, Batteries, Cables", total_items: 1275 },
    ],
  },
  {
    category: "Textiles",
    count: 14,
    total_items: 3428,
    chapters: [
      { chapter: "52", description: "Cotton (Raw cotton bales, Cotton yarn, Grey fabrics)", total_items: 480 },
      { chapter: "61", description: "Articles of apparel and clothing accessories, knitted (T-shirts)", total_items: 520 },
      { chapter: "62", description: "Articles of apparel and clothing accessories, woven", total_items: 580 },
      { chapter: "63", description: "Other made up textile articles (Face masks, Linen)", total_items: 310 },
    ],
  },
  {
    category: "Chemicals",
    count: 6,
    total_items: 3383,
    chapters: [
      { chapter: "28", description: "Inorganic chemicals (Caustic soda, Sulphur)", total_items: 620 },
      { chapter: "29", description: "Organic chemicals (Citric acid, Acetic acid)", total_items: 1240 },
      { chapter: "31", description: "Fertilisers (Urea, DAP, Potash)", total_items: 140 },
    ],
  },
  {
    category: "Packaging",
    count: 2,
    total_items: 978,
    chapters: [
      { chapter: "48", description: "Paper and paperboard; Corrugated cartons, Kraft paper", total_items: 410 },
      { chapter: "39", description: "Plastics and articles thereof (Polymers, Nitrile gloves, Packaging)", total_items: 568 },
    ],
  },
  {
    category: "Healthcare & Medical",
    count: 2,
    total_items: 850,
    chapters: [
      { chapter: "30", description: "Pharmaceutical products (Medicines, Vaccines)", total_items: 390 },
      { chapter: "90", description: "Medical, surgical and dental instruments", total_items: 460 },
    ],
  },
  {
    category: "Services",
    count: 1,
    total_items: 681,
    chapters: [
      { chapter: "99", description: "Services (SAC: Construction, IT, Transport, Logistics)", total_items: 681 },
    ],
  },
];

export function HsnTreeSelector({
  selectedHsnCode,
  onSelect,
  role = "buyer",
}: HsnTreeSelectorProps) {
  // Tree categories list
  const [categories, setCategories] = useState<HsnCategoryGroup[]>(FALLBACK_CATEGORIES);
  const [loadingCategories, setLoadingCategories] = useState(false);

  // Search state
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<HsnItem[]>([]);
  const [isSearching, setIsSearching] = useState(false);
  const [isSearchLoading, setIsSearchLoading] = useState(false);
  const searchTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Expansion states
  const [expandedCategories, setExpandedCategories] = useState<Set<string>>(
    new Set(["Agriculture"])
  );
  const [expandedChapters, setExpandedChapters] = useState<Set<string>>(
    new Set(["07"])
  );
  const [expandedHeadings, setExpandedHeadings] = useState<Set<string>>(
    new Set(["0713"])
  );
  const [expandedSubheadings, setExpandedSubheadings] = useState<Set<string>>(
    new Set(["071331"])
  );

  // Dynamic child node caches
  const [headingsCache, setHeadingsCache] = useState<Record<string, HsnItem[]>>({});
  const [subheadingsCache, setSubheadingsCache] = useState<Record<string, HsnItem[]>>({});
  const [tariffItemsCache, setTariffItemsCache] = useState<Record<string, HsnItem[]>>({});
  const [loadingNodes, setLoadingNodes] = useState<Set<string>>(new Set());

  // Selected item
  const [selectedItem, setSelectedItem] = useState<(HsnSelection & { description?: string }) | null>(null);

  const [isTreeOpen, setIsTreeOpen] = useState(true);

  // Custom HSN mode
  const [isCustomMode, setIsCustomMode] = useState(false);
  const [customHsn, setCustomHsn] = useState("");
  const [customProduct, setCustomProduct] = useState("");
  const [customCategory, setCustomCategory] = useState("General Goods");
  const [aiSearchSuggestion, setAiSearchSuggestion] = useState<HsnSuggestion | null>(null);

  // Load official categories on mount
  useEffect(() => {
    let active = true;
    setLoadingCategories(true);
    hsnApi
      .getCategories()
      .then((data) => {
        if (active && data && data.length > 0) {
          setCategories(data);
        }
      })
      .catch((err) => {
        console.warn("Could not fetch remote HSN categories, using fallback seed:", err);
      })
      .finally(() => {
        if (active) setLoadingCategories(false);
      });

    return () => {
      active = false;
    };
  }, []);

  // Pre-fetch Chapter 07 headings and 0713 subheadings on mount for instant zero-latency view
  useEffect(() => {
    void fetchHeadings("07");
    void fetchSubheadings("0713");
    void fetchTariffItems("071331");
  }, []);

  // Sync initial or prop-driven selected code
  useEffect(() => {
    if (!selectedHsnCode) return;
    const clean = selectedHsnCode.replace(/[^\d]/g, "");
    if (!clean) return;

    let active = true;
    hsnApi
      .getByCode(clean)
      .then((item) => {
        if (!active || !item) return;
        const sel: HsnSelection = {
          hsnCode: item.code,
          normalizedCode: item.code.replace(/[^\d]/g, ""),
          productName: item.formatted_title || item.description,
          category: item.category,
          fullName: `HSN ${item.code}: ${item.formatted_title || item.description}`,
          path: [
            item.category,
            `Chapter ${item.chapter}`,
            `Heading ${item.heading}`,
            item.formatted_title || item.description,
          ],
        };
        setSelectedItem({ ...sel, description: item.description });
      })
      .catch(() => {
        if (active) {
          setCustomHsn(selectedHsnCode);
        }
      });

    return () => {
      active = false;
    };
  }, [selectedHsnCode]);

  // Debounced search across all 22,616 official codes in HSN_SAC.xlsx
  useEffect(() => {
    const query = searchQuery.trim();
    if (!query) {
      setIsSearching(false);
      setSearchResults([]);
      setIsSearchLoading(false);
      return;
    }

    setIsSearching(true);
    setIsSearchLoading(true);

    if (searchTimeoutRef.current) {
      clearTimeout(searchTimeoutRef.current);
    }

    searchTimeoutRef.current = setTimeout(() => {
      hsnApi
        .search(query, 60)
        .then((items) => {
          setSearchResults(items || []);
        })
        .catch((err) => {
          console.error("HSN search failed:", err);
          setSearchResults([]);
        })
        .finally(() => {
          setIsSearchLoading(false);
        });

      void hsnApi
        .suggest(query)
        .then((sug) => {
          setAiSearchSuggestion(sug);
        })
        .catch(() => setAiSearchSuggestion(null));
    }, 200);

    return () => {
      if (searchTimeoutRef.current) clearTimeout(searchTimeoutRef.current);
    };
  }, [searchQuery]);

  // Tree toggle handlers
  function toggleCategory(catName: string) {
    setExpandedCategories((prev) => {
      const next = new Set(prev);
      if (next.has(catName)) next.delete(catName);
      else next.add(catName);
      return next;
    });
  }

  const fetchHeadings = useCallback(async (chapterCode: string) => {
    if (headingsCache[chapterCode]) return;
    setLoadingNodes((prev) => new Set([...prev, `ch_${chapterCode}`]));
    try {
      const items = await hsnApi.getChildren(chapterCode);
      setHeadingsCache((prev) => ({ ...prev, [chapterCode]: items }));
    } catch (err) {
      console.error(`Failed to load headings for chapter ${chapterCode}:`, err);
    } finally {
      setLoadingNodes((prev) => {
        const next = new Set(prev);
        next.delete(`ch_${chapterCode}`);
        return next;
      });
    }
  }, [headingsCache]);

  function toggleChapter(chapter: HsnChapterItem) {
    const ch = chapter.chapter;
    setExpandedChapters((prev) => {
      const next = new Set(prev);
      if (next.has(ch)) {
        next.delete(ch);
      } else {
        next.add(ch);
        void fetchHeadings(ch);
      }
      return next;
    });
  }

  const fetchSubheadings = useCallback(async (headingCode: string) => {
    if (subheadingsCache[headingCode]) return;
    setLoadingNodes((prev) => new Set([...prev, `hd_${headingCode}`]));
    try {
      const items = await hsnApi.getChildren(headingCode);
      setSubheadingsCache((prev) => ({ ...prev, [headingCode]: items }));
    } catch (err) {
      console.error(`Failed to load subheadings for heading ${headingCode}:`, err);
    } finally {
      setLoadingNodes((prev) => {
        const next = new Set(prev);
        next.delete(`hd_${headingCode}`);
        return next;
      });
    }
  }, [subheadingsCache]);

  function toggleHeading(heading: HsnItem) {
    const code = heading.code;
    setExpandedHeadings((prev) => {
      const next = new Set(prev);
      if (next.has(code)) {
        next.delete(code);
      } else {
        next.add(code);
        void fetchSubheadings(code);
      }
      return next;
    });
  }

  const fetchTariffItems = useCallback(async (subheadingCode: string) => {
    if (tariffItemsCache[subheadingCode]) return;
    setLoadingNodes((prev) => new Set([...prev, `sub_${subheadingCode}`]));
    try {
      const items = await hsnApi.getChildren(subheadingCode);
      setTariffItemsCache((prev) => ({ ...prev, [subheadingCode]: items }));
    } catch (err) {
      console.error(`Failed to load tariff items for subheading ${subheadingCode}:`, err);
    } finally {
      setLoadingNodes((prev) => {
        const next = new Set(prev);
        next.delete(`sub_${subheadingCode}`);
        return next;
      });
    }
  }, [tariffItemsCache]);

  function toggleSubheading(subheading: HsnItem) {
    const code = subheading.code;
    setExpandedSubheadings((prev) => {
      const next = new Set(prev);
      if (next.has(code)) {
        next.delete(code);
      } else {
        next.add(code);
        void fetchTariffItems(code);
      }
      return next;
    });
  }

  // Selection Handler
  function handleSelect(item: HsnItem, pathContext?: string[]) {
    const productName = item.formatted_title || item.description;
    const path = pathContext || [
      item.category,
      item.chapter ? `Chapter ${item.chapter}` : "",
      item.heading ? `Heading ${item.heading}` : "",
      productName,
    ].filter(Boolean);

    const selection: HsnSelection = {
      hsnCode: item.code,
      normalizedCode: item.code.replace(/[^\d]/g, ""),
      productName,
      category: item.category,
      fullName: `HSN ${item.code}: ${productName}`,
      path,
    };

    setSelectedItem({ ...selection, description: item.description });
    setIsCustomMode(false);
    setIsTreeOpen(false);

    onSelect(selection);
  }

  // Custom Code Submission
  function handleApplyCustom() {
    if (!customHsn.trim() || !customProduct.trim()) return;

    const norm = customHsn.replace(/[^\d]/g, "");
    const selection: HsnSelection = {
      hsnCode: customHsn.trim(),
      normalizedCode: norm,
      productName: customProduct.trim(),
      category: customCategory.trim() || "General Goods",
      fullName: `HSN ${customHsn.trim()}: ${customProduct.trim()}`,
      path: [customCategory.trim() || "General Goods", `HSN ${customHsn.trim()}`, customProduct.trim()],
    };

    setSelectedItem(selection);
    setIsTreeOpen(false);
    onSelect(selection);
  }

  return (
    <div className="hsn-selector-container">
      {/* Selected Commodity Confirmation Card */}
      {selectedItem && (
        <div className="hsn-selected-card">
          <div className="hsn-selected-header">
            <div className="hsn-selected-badge">
              <IconCheck size={14} className="hsn-check-icon" />
              <span>HSN {selectedItem.hsnCode}</span>
            </div>
            <span className="hsn-category-tag">{selectedItem.category}</span>
          </div>

          <div className="hsn-selected-body">
            <h3 className="hsn-selected-title">{selectedItem.productName}</h3>
            {selectedItem.description && selectedItem.description !== selectedItem.productName && (
              <p className="hsn-selected-desc">{selectedItem.description}</p>
            )}
            <div className="hsn-breadcrumbs">
              {selectedItem.path.map((step, idx) => (
                <span key={idx} className="hsn-crumb">
                  {idx > 0 && <span className="hsn-crumb-sep">&rsaquo;</span>}
                  {step}
                </span>
              ))}
            </div>
          </div>

          <div className="hsn-selected-actions">
            <button
              type="button"
              className="button ghost small-btn"
              onClick={() => setIsTreeOpen((prev) => !prev)}
            >
              {isTreeOpen ? "Close Catalog" : "Change HSN Selection"}
            </button>
          </div>
        </div>
      )}

      {/* Main Tree Explorer (Collapsible if an item is already selected) */}
      {(!selectedItem || isTreeOpen) && (
        <div className="hsn-tree-panel">
          <div className="hsn-tree-header">
            <div className="hsn-tree-title-row">
              <span className="hsn-tree-title">
                <IconSparkles size={16} /> Select Product by Official HSN Code
              </span>
              <span className="hsn-tree-subtitle">
                {role === "seller"
                  ? "Select the exact commodity you are selling"
                  : "Select the exact commodity you need to buy"}{" "}
                (Category &rarr; Chapter &rarr; Heading &rarr; Tariff Item)
              </span>
            </div>

            {/* Quick Instant Search Bar */}
            <div className="hsn-search-wrapper">
              <IconSearch size={16} className="hsn-search-icon" />
              <input
                type="text"
                className="hsn-search-input"
                placeholder="Search across 22,616+ official HSN codes (e.g. 'moong', 'chickpea', '0713', 'solar', 'steel', '9954')..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
              />
              {searchQuery && (
                <button
                  type="button"
                  className="hsn-search-clear"
                  onClick={() => setSearchQuery("")}
                  title="Clear search"
                >
                  <IconClose size={14} />
                </button>
              )}
            </div>
          </div>

          {/* Search Results Flat View */}
          {isSearching ? (
            <div className="hsn-search-results">
              <div className="hsn-results-count">
                {isSearchLoading ? (
                  <span>Searching official HSN/SAC master database...</span>
                ) : (
                  <span>
                    Found {searchResults.length} matching {searchResults.length === 1 ? "commodity" : "commodities"} for &ldquo;{searchQuery}&rdquo;:
                  </span>
                )}
              </div>

              {/* AI Semantic Brand Match Banner */}
              {aiSearchSuggestion && (
                <div className="hsn-ai-search-banner">
                  <div className="hsn-ai-banner-header">
                    <IconSparkles size={14} className="hsn-sparkle-icon" />
                    <span>AI Brand Classification Match for &ldquo;{searchQuery}&rdquo;:</span>
                  </div>
                  <div className="hsn-ai-banner-content">
                    <span className="hsn-ai-banner-code">HSN {aiSearchSuggestion.code}</span> &bull;{" "}
                    <strong>{aiSearchSuggestion.formatted_title}</strong>
                    <span className="hsn-banner-cat">({aiSearchSuggestion.category} &bull; Ch. {aiSearchSuggestion.chapter})</span>
                  </div>
                  {aiSearchSuggestion.reason && (
                    <div className="hsn-banner-reason">💡 {aiSearchSuggestion.reason}</div>
                  )}
                  <button
                    type="button"
                    className="button primary small-btn hsn-banner-btn"
                    onClick={() => {
                      handleSelect(
                        {
                          code: aiSearchSuggestion.code,
                          description: aiSearchSuggestion.description,
                          formatted_title: aiSearchSuggestion.formatted_title,
                          level: 4,
                          chapter: aiSearchSuggestion.chapter,
                          heading: aiSearchSuggestion.code.slice(0, 4),
                          subheading: aiSearchSuggestion.code.length >= 6 ? aiSearchSuggestion.code.slice(0, 6) : "",
                          category: aiSearchSuggestion.category,
                          is_service: false,
                          aliases: aiSearchSuggestion.reason || "",
                          has_children: false,
                        },
                        [aiSearchSuggestion.category, `Chapter ${aiSearchSuggestion.chapter}`, aiSearchSuggestion.formatted_title]
                      );
                    }}
                  >
                    Select HSN {aiSearchSuggestion.code}
                  </button>
                </div>
              )}

              {!isSearchLoading && searchResults.length === 0 && !aiSearchSuggestion ? (
                <div className="hsn-empty-search">
                  <p>No official tariff commodity found matching &ldquo;{searchQuery}&rdquo;.</p>
                  <button
                    type="button"
                    className="button secondary small-btn"
                    onClick={() => {
                      setIsCustomMode(true);
                      setCustomProduct(searchQuery);
                    }}
                  >
                    Enter Custom HSN Code
                  </button>
                </div>
              ) : (
                <ul className="hsn-result-list">
                  {searchResults.map((item) => {
                    const isSelected = selectedItem?.hsnCode === item.code;
                    return (
                      <li
                        key={`${item.code}_${item.level}`}
                        className={`hsn-result-item ${isSelected ? "selected" : ""}`}
                        onClick={() => handleSelect(item)}
                      >
                        <div className="hsn-result-code">
                          <span className="badge-code">
                            {item.is_service ? "SAC" : "HSN"} {item.code}
                          </span>
                          <span className="badge-cat">{item.category}</span>
                        </div>
                        <div className="hsn-result-content">
                          <strong>{item.formatted_title || item.description}</strong>
                          <span className="hsn-result-sub">
                            Level {item.level} · Chapter {item.chapter}
                            {item.heading && item.heading !== item.chapter ? ` · Heading ${item.heading}` : ""}
                          </span>
                          {item.description && item.description !== item.formatted_title && (
                            <small className="hsn-result-desc">{item.description}</small>
                          )}
                          {item.aliases && (
                            <div className="hsn-aliases-tag">
                              {item.aliases.startsWith("Brand match:") ? (
                                <span>💡 {item.aliases}</span>
                              ) : (
                                <span>Trade terms: {item.aliases}</span>
                              )}
                            </div>
                          )}
                        </div>
                        <button
                          type="button"
                          className="button primary small-btn hsn-select-btn"
                          onClick={(e) => {
                            e.stopPropagation();
                            handleSelect(item);
                          }}
                        >
                          Select
                        </button>
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>
          ) : (
            /* Hierarchical Tree Browser View */
            <div className="hsn-tree-browser">
              {loadingCategories && (
                <div className="hsn-loading-indicator">Loading official HSN taxonomy...</div>
              )}

              {categories.map((cat) => {
                const isCatExpanded = expandedCategories.has(cat.category);
                return (
                  <div key={cat.category} className="hsn-tree-category-node">
                    {/* Level 1: Category */}
                    <button
                      type="button"
                      className="hsn-tree-category-btn"
                      onClick={() => toggleCategory(cat.category)}
                    >
                      <span className="hsn-expand-icon">
                        {isCatExpanded ? (
                          <IconChevronDown size={16} />
                        ) : (
                          <IconChevronRight size={16} />
                        )}
                      </span>
                      <span className="hsn-category-name">{cat.category}</span>
                      <span className="hsn-count-badge">
                        {cat.total_items ? `${cat.total_items.toLocaleString()} codes` : `${cat.count} chapters`}
                      </span>
                    </button>

                    {/* Level 2: Chapters */}
                    {isCatExpanded && (
                      <div className="hsn-chapters-container">
                        {cat.chapters.map((ch) => {
                          const isChExpanded = expandedChapters.has(ch.chapter);
                          const headings = headingsCache[ch.chapter] || [];
                          const isLoadingHeadings = loadingNodes.has(`ch_${ch.chapter}`);

                          return (
                            <div key={ch.chapter} className="hsn-chapter-node">
                              <div className="hsn-chapter-row">
                                <button
                                  type="button"
                                  className="hsn-chapter-btn"
                                  onClick={() => toggleChapter(ch)}
                                >
                                  <span className="hsn-expand-icon">
                                    {isChExpanded ? (
                                      <IconChevronDown size={14} />
                                    ) : (
                                      <IconChevronRight size={14} />
                                    )}
                                  </span>
                                  <span className="hsn-chapter-code">Ch. {ch.chapter}</span>
                                  <span className="hsn-chapter-desc">{ch.description}</span>
                                  {ch.total_items && (
                                    <span className="hsn-mini-count">{ch.total_items} items</span>
                                  )}
                                </button>
                                <button
                                  type="button"
                                  className="button ghost small-btn hsn-node-select-btn"
                                  title={`Select Chapter ${ch.chapter}`}
                                  onClick={() =>
                                    handleSelect(
                                      {
                                        code: ch.chapter,
                                        description: ch.description,
                                        level: 2,
                                        chapter: ch.chapter,
                                        heading: "",
                                        subheading: "",
                                        category: cat.category,
                                        is_service: false,
                                        formatted_title: ch.description,
                                        has_children: true,
                                      },
                                      [cat.category, `Chapter ${ch.chapter}`]
                                    )
                                  }
                                >
                                  Select Ch. {ch.chapter}
                                </button>
                              </div>

                              {/* Level 3: Headings */}
                              {isChExpanded && (
                                <div className="hsn-headings-container">
                                  {isLoadingHeadings && (
                                    <div className="hsn-node-loading">Loading headings...</div>
                                  )}

                                  {headings.map((hd) => {
                                    const isHdExpanded = expandedHeadings.has(hd.code);
                                    const subheadings = subheadingsCache[hd.code] || [];
                                    const isLoadingSub = loadingNodes.has(`hd_${hd.code}`);

                                    return (
                                      <div key={hd.code} className="hsn-heading-node">
                                        <div className="hsn-heading-row">
                                          <button
                                            type="button"
                                            className="hsn-heading-btn"
                                            onClick={() => toggleHeading(hd)}
                                          >
                                            <span className="hsn-expand-icon">
                                              {isHdExpanded ? (
                                                <IconChevronDown size={13} />
                                              ) : (
                                                <IconChevronRight size={13} />
                                              )}
                                            </span>
                                            <span className="hsn-heading-code">{hd.code}</span>
                                            <span className="hsn-heading-name">
                                              {hd.formatted_title || hd.description}
                                            </span>
                                          </button>
                                          <button
                                            type="button"
                                            className="button ghost small-btn hsn-node-select-btn"
                                            title={`Select Heading ${hd.code}`}
                                            onClick={() =>
                                              handleSelect(hd, [cat.category, `Chapter ${ch.chapter}`, hd.formatted_title || hd.description])
                                            }
                                          >
                                            Select {hd.code}
                                          </button>
                                        </div>

                                        {/* Level 4: Subheadings / Commodities */}
                                        {isHdExpanded && (
                                          <div className="hsn-subheadings-container">
                                            {isLoadingSub && (
                                              <div className="hsn-node-loading">Loading commodities...</div>
                                            )}

                                            {subheadings.map((sub) => {
                                              const isSubExpanded = expandedSubheadings.has(sub.code);
                                              const tariffItems = tariffItemsCache[sub.code] || [];
                                              const isLoadingTariff = loadingNodes.has(`sub_${sub.code}`);
                                              const isLeaf = !sub.has_children;

                                              return (
                                                <div key={sub.code} className="hsn-subheading-node">
                                                  <div className="hsn-subheading-row">
                                                    {isLeaf ? (
                                                      <div
                                                        className={`hsn-commodity-item ${
                                                          selectedItem?.hsnCode === sub.code ? "selected" : ""
                                                        }`}
                                                        onClick={() =>
                                                          handleSelect(sub, [
                                                            cat.category,
                                                            `Chapter ${ch.chapter}`,
                                                            `Heading ${hd.code}`,
                                                            sub.formatted_title || sub.description,
                                                          ])
                                                        }
                                                      >
                                                        <span className="hsn-commodity-code-pill">
                                                          {sub.code}
                                                        </span>
                                                        <div className="hsn-commodity-info">
                                                          <span className="hsn-commodity-prod">
                                                            {sub.formatted_title || sub.description}
                                                          </span>
                                                          {sub.description && sub.description !== sub.formatted_title && (
                                                            <span className="hsn-commodity-detail">
                                                              {sub.description}
                                                            </span>
                                                          )}
                                                        </div>
                                                        <button
                                                          type="button"
                                                          className="button primary small-btn"
                                                          onClick={(e) => {
                                                            e.stopPropagation();
                                                            handleSelect(sub, [
                                                              cat.category,
                                                              `Chapter ${ch.chapter}`,
                                                              `Heading ${hd.code}`,
                                                              sub.formatted_title || sub.description,
                                                            ]);
                                                          }}
                                                        >
                                                          Select
                                                        </button>
                                                      </div>
                                                    ) : (
                                                      <>
                                                        <button
                                                          type="button"
                                                          className="hsn-subheading-btn"
                                                          onClick={() => toggleSubheading(sub)}
                                                        >
                                                          <span className="hsn-expand-icon">
                                                            {isSubExpanded ? (
                                                              <IconChevronDown size={12} />
                                                            ) : (
                                                              <IconChevronRight size={12} />
                                                            )}
                                                          </span>
                                                          <span className="hsn-subheading-code">
                                                            {sub.code}
                                                          </span>
                                                          <span className="hsn-subheading-name">
                                                            {sub.formatted_title || sub.description}
                                                          </span>
                                                        </button>
                                                        <button
                                                          type="button"
                                                          className="button ghost small-btn hsn-node-select-btn"
                                                          onClick={() =>
                                                            handleSelect(sub, [
                                                              cat.category,
                                                              `Chapter ${ch.chapter}`,
                                                              `Heading ${hd.code}`,
                                                              sub.formatted_title || sub.description,
                                                            ])
                                                          }
                                                        >
                                                          Select {sub.code}
                                                        </button>
                                                      </>
                                                    )}
                                                  </div>

                                                  {/* Level 5: 8-digit Tariff Items */}
                                                  {!isLeaf && isSubExpanded && (
                                                    <ul className="hsn-commodities-list">
                                                      {isLoadingTariff && (
                                                        <li className="hsn-node-loading">Loading tariff items...</li>
                                                      )}
                                                      {tariffItems.map((tItem) => {
                                                        const isSelected = selectedItem?.hsnCode === tItem.code;
                                                        return (
                                                          <li
                                                            key={tItem.code}
                                                            className={`hsn-commodity-item ${
                                                              isSelected ? "selected" : ""
                                                            }`}
                                                            onClick={() =>
                                                              handleSelect(tItem, [
                                                                cat.category,
                                                                `Chapter ${ch.chapter}`,
                                                                `Heading ${hd.code}`,
                                                                sub.formatted_title || sub.description,
                                                                tItem.formatted_title || tItem.description,
                                                              ])
                                                            }
                                                          >
                                                            <div className="hsn-commodity-code-pill">
                                                              {tItem.code}
                                                            </div>
                                                            <div className="hsn-commodity-info">
                                                              <span className="hsn-commodity-prod">
                                                                {tItem.formatted_title || tItem.description}
                                                              </span>
                                                              {tItem.description && tItem.description !== tItem.formatted_title && (
                                                                <span className="hsn-commodity-detail">
                                                                  {tItem.description}
                                                                </span>
                                                              )}
                                                            </div>
                                                            <button
                                                              type="button"
                                                              className="button primary small-btn"
                                                              onClick={(e) => {
                                                                e.stopPropagation();
                                                                handleSelect(tItem, [
                                                                  cat.category,
                                                                  `Chapter ${ch.chapter}`,
                                                                  `Heading ${hd.code}`,
                                                                  sub.formatted_title || sub.description,
                                                                  tItem.formatted_title || tItem.description,
                                                                ]);
                                                              }}
                                                            >
                                                              Select
                                                            </button>
                                                          </li>
                                                        );
                                                      })}
                                                    </ul>
                                                  )}
                                                </div>
                                              );
                                            })}
                                          </div>
                                        )}
                                      </div>
                                    );
                                  })}
                                </div>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}

          {/* Custom HSN Modal/Toggle */}
          <div className="hsn-custom-footer">
            {!isCustomMode ? (
              <button
                type="button"
                className="button ghost small-btn"
                onClick={() => setIsCustomMode(true)}
              >
                + My exact product/HSN code isn&apos;t listed (Enter manually)
              </button>
            ) : (
              <div className="hsn-custom-box">
                <div className="hsn-custom-head">
                  <strong>Enter Custom HSN Code</strong>
                  <button
                    type="button"
                    className="button ghost small-btn"
                    onClick={() => setIsCustomMode(false)}
                  >
                    Cancel
                  </button>
                </div>
                <div className="field-grid">
                  <div className="field">
                    <label>HSN Code (digits only, e.g. 071331)</label>
                    <input
                      type="text"
                      placeholder="e.g. 071331"
                      value={customHsn}
                      onChange={(e) => setCustomHsn(e.target.value)}
                    />
                  </div>
                  <div className="field">
                    <label>Product Name</label>
                    <input
                      type="text"
                      placeholder="e.g. Sabut Moong Organic"
                      value={customProduct}
                      onChange={(e) => setCustomProduct(e.target.value)}
                    />
                  </div>
                </div>
                <div className="field">
                  <label>Marketplace Category</label>
                  <select
                    value={customCategory}
                    onChange={(e) => setCustomCategory(e.target.value)}
                  >
                    <option value="Agriculture">Agriculture</option>
                    <option value="Electronics">Electronics</option>
                    <option value="Textiles">Textiles</option>
                    <option value="Automotive">Automotive</option>
                    <option value="Chemicals">Chemicals</option>
                    <option value="Packaging">Packaging</option>
                    <option value="Industrial">Industrial</option>
                    <option value="Construction">Construction</option>
                    <option value="Healthcare & Medical">Healthcare &amp; Medical</option>
                    <option value="Services">Services</option>
                  </select>
                </div>
                <button
                  type="button"
                  className="button primary small-btn"
                  onClick={handleApplyCustom}
                  disabled={!customHsn.trim() || !customProduct.trim()}
                >
                  Apply Custom HSN
                </button>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
