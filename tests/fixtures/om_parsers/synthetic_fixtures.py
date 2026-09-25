"""Deterministic OM parser fixtures (public release).

These are hand-built synthetic pdftotext-style layouts that exercise the
same parser sections and code paths as the private regression corpus
(generic_table_om_v1 and generic_debt_guidance_v1 families) without
shipping real broker documents. Numbers are synthetic; the structural shape
(labels, column order, section order) mirrors real OM layouts so the parser
runs its production paths.

Regenerate with: python tests/fixtures/om_parsers/synthetic_fixtures.py
"""

from pathlib import Path

FIXTURE_ROOT = Path(__file__).resolve().parent

MAPLE_GROVE_OM = """\
PROPERTY SUMMARY
A DDRES S
742 Sample Ave, Demo City, ST 00000
FOO
 Y E A R BUILT
1984
TOTAL UNITS
238

INCOME:
Gross Scheduled Market Rent
Plus: Renovated Unit Premiums
Gain / Loss to Lease
Gross Potential Rent

AMOUNT
Amount
$5,355,600
$0
($891,636)
$4,463,964

T3 REVENUE/T12 EXPENSES
% OF GPR
GPR
120%
0%
-20.0%
100.0%

PER UNIT
Unit
$22,503
$0
($3,746)
$18,756

AMOUNT
Amount
$4,354,709
$87,267
$15,697
$4,457,673

PROFORMA
% OF GPR
GPR
98%
2%
0.4%
100.0%

PER UNIT
Unit
$18,297
$367
$66
$18,730

Less: Vacancy
Less: Concessions
Less: Non-Revenue Units
Less: Bad Debt
Net Rental Income

($392,067)
($38,953)
($30,918)
($30,382)
$3,971,643

-8.8%
-0.9%
-0.7%
-0.7%
89.0%

($1,647)
($164)
($130)
($128)
$16,688

($222,884)
($44,577)
($28,095)
($22,288)
$4,139,830

-5.0%
-1.0%
-0.6%
-0.5%
92.9%

($936)
($187)
($118)
($94)
$17,394

Plus: Utility Reimbursements
Plus: Parking/Storage Income
Plus: Internet Package
Plus: Other Income
Total Income
Monthly Collections

$305,776
$8,703
$0
$143,367
$4,429,489
$369,124

6.8%
0.2%
0.0%
3.2%
99.2%

$1,285
$37
$0
$602
$18,611

$319,634
$10,080
$78,540
$192,098
$4,740,182
$395,015

7.2%
0.2%
1.8%
4.3%
106.3%

$1,343
$42
$330
$807
$19,917

EXPENSES:
Utilities
Repairs and Maintenance
Apartment Make Ready
Contract Services
Marketing
Payroll Expenses
General and Administrative
Property Taxes
Franchise Tax
Insurance
Management Fees
Miscellaneous
Total Expenses

$299,219
$160,936
$98,493
$176,012
$49,122
$498,451
$105,791
$752,437
$14,951
$298,787
$190,125
$0
$2,644,324

6.7%
3.6%
2.2%
3.9%
1.1%
11.2%
2.4%
16.9%
0.3%
6.7%
4.3%
0.0%
59.2%

$1,257
$676
$414
$740
$206
$2,094
$444
$3,162
$63
$1,255
$799
$0
$11,111

$299,219
$119,000
$83,300
$176,012
$47,600
$403,000
$105,791
$752,437
$15,690
$166,600
$142,205
$0
$2,310,854

6.7%
2.7%
1.9%
3.9%
1.1%
9.0%
2.4%
16.9%
0.4%
3.7%
3.2%
0.0%
51.8%

$1,257
$500
$350
$740
$200
$1,693
$444
$3,162
$66
$700
$598
$0
$9,709

Net Operating Income
Replacement Reserves
Net Cash Flow

$1,785,165
$0
$1,785,165

40.0%
0.0%
40.0%

$7,501
$0
$7,501

$2,429,328
($59,500)
$2,369,828

54.5%
-1.3%
53.2%

$10,207
($250)
$9,957

PROPERTY TAXES
TAXING JURISDICTION
TAX RATE PER $100
Sample City
0.5375
Sample ISD
0.9481
Sample County
0.2155
Sample College
0.106575
Sample Hospital
0.212
*Per SampleCAD
0.02019675
**Property Account #: 14006580000000000
2026 Tax Rate
The WDIS Proforma assumes $1,693/unit for Payroll.
The WDIS Proforma assumes Replacement Reserves of $250/unit.
RENT ROLL DATA FOLLOWS
"""

MAPLE_GROVE_DEBT_GUIDANCE = """\
DEBT GUIDANCE — The Demo at Maple Grove
Prepared by the mortgage brokerage team for the prospective buyer.

Proposed Financing Terms, Loan Option A:
  Benchmark Rate: hold-matched Treasury plus a spread of 150 bps.
  All-in-Rate: 4.60% fixed, 25-year amortization, 5-year term.
  Max LTV: 70% at Min DSCR 1.25x on UW NOI.
  Loan Option B: 5.00% fixed, same structure.
"""

WILLOW_COURT_PROFORMA = """\
Property Description
Willow Court
11 Example Ln | Demoville, ST

                                  ASSET SUMMARY
 Built                                                                                    2023     County                                                      Sample County, Demo Tax District
 Units                                                                                      272    Tax Millage Rate (2025)                                                                   58.0000

Unit Mix

Pro Forma
                                                                    BROKER          MARCH 2026            MARCH 2026                MARCH 2026
INCOME
                                                                   PROFORMA         T3 STABILIZED       T3 ANNUALIZED                 T12 INCOME
SCHEDULED RENT                                                      $4,816,704         $4,816,704           $4,816,704                  $4,816,704
  Less: Loss-to-Lease                                    0.50%        ($24,084)           ($43,624)            ($43,624)                   $46,935
  Less: Vacancy                                          6.00%      ($289,002)         ($385,336)             ($531,899)                  ($688,317)
  Less: Concessions                                      1.00%        ($48,167)        ($192,668)           ($269,027)                  ($645,246)
  Less: Bad Debt                                          1.00%        ($48,167)        ($120,418)          ($459,099)                  ($345,410)
  Less: Model Units                                      0.40%          ($19,188)           ($19,188)            ($19,188)                   ($19,188)
NET RENTAL INCOME                                                   $4,706,580         $4,055,470           $3,493,867                  $3,165,478
  Plus: Fee Income                                                    $271,648          $206,405              $206,405                    $263,736
  Plus: Water/Sewer/Trash/Pest/Bulk Internet Income                   $433,152          $408,404              $408,404                    $399,812
  Plus: Valet Trash Income                                             $27,744                $0                    $0                           $0
  Plus: Resident Insurance                                             $50,901           $40,664              $40,664                    $31,133
  Plus: Other Income                                                   $12,204            $6,502               $6,502                    $23,870
TOTAL OPERATING INCOME                                              $5,301,798         $4,668,040            $4,109,090                  $3,884,073

OPERATING EXPENSES
  Real Estate Taxes                                                  $509,856           $509,856              $509,856                    $509,856
  Insurance                                                          $145,444           $145,444              $145,444                    $145,444
  Utilities (Electric/Gas)                                           $180,000           $180,000              $180,000                    $180,000
  Utilities (Water/Sewer)                                            $130,000           $130,000              $130,000                    $130,000
  Payroll                                                            $490,000           $490,000              $490,000                    $490,000
  Repairs & Maintenance/Turnover                                     $310,000           $310,000              $310,000                    $310,000
  Grounds & Landscaping                                              $60,000           $60,000              $60,000                    $60,000
  Pest Control/Trash                                                 $40,000           $40,000              $40,000                    $40,000
  Administrative                                                     $260,000           $260,000              $260,000                    $260,000
  Advertising & Promotion                                             $40,000           $40,000              $40,000                    $40,000
  Management Fee                                                     $164,000           $164,000              $164,000                    $164,000
  TOTAL EXPENSES                                                   $2,329,300         $2,329,300            $2,329,300                  $2,329,300
  NET OPERATING INCOME                                              $2,972,498         $2,338,740            $1,779,790                  $1,554,773

Income Notes
Berkadia pro forma layout (synthetic).
"""


def write_fixtures() -> None:
    (FIXTURE_ROOT / "maple_grove_om.txt").write_text(MAPLE_GROVE_OM)
    (FIXTURE_ROOT / "maple_grove_debt_guidance.txt").write_text(MAPLE_GROVE_DEBT_GUIDANCE)
    (FIXTURE_ROOT / "willow_court_berkadia_proforma.txt").write_text(WILLOW_COURT_PROFORMA)


if __name__ == "__main__":
    write_fixtures()