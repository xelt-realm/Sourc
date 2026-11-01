  43    44 newline       \n | \n
  44    47 keyword       DIM | DIM
  48    54 ident         nombre | nombre
  55    57 keyword       AS | AS
  58    64 keyword       STRING | STRING
  65    66 op            = | =
  68    74 string        mundo | mundo
  74    75 newline       \n | \n
  75    78 keyword       DIM | DIM
  79    84 ident         veces | veces
  85    87 keyword       AS | AS
  88    95 keyword       INTEGER | INTEGER
  96    97 op            = | =
  98    99 int           3 | 3
  99   100 newline       \n | \n
 100   105 keyword       PRINT | PRINT
 107   112 string        hola  | hola 
 112   113 op            { | {
 113   119 ident         nombre | nombre
 119   120 op            } | }
 121   122 newline       \n | \n
 122   127 keyword       PRINT | PRINT
 129   140 string        calculado:  | calculado: 
 140   141 op            { | {
 141   142 int           1 | 1
 143   144 op            + | +
 145   146 int           2 | 2
 147   148 op            * | *
 149   150 int           3 | 3
 150   151 op            } | }
 152   153 newline       \n | \n
 153   158 keyword       PRINT | PRINT
 160   169 string        anidado:  | anidado: 
 169   170 op            { | {
 170   176 ident         nombre | nombre
 176   177 op            } | }
 177   180 string         x  |  x 
 180   181 op            { | {
 181   186 ident         veces | veces
 186   187 op            } | }
 188   189 newline       \n | \n
 189   194 keyword       PRINT | PRINT
 196   219 string        literal: {corchetes} | literal: {corchetes}
 219   220 newline       \n | \n
 220   220 eof            | 

; 45 token(s)
