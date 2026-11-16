# System og UART · PYNQ-Z2

Diagrammerne følger den aktuelle `ps_uart_integration.zip`. UART-testen og et CNN-svar fra labbets accelerator er afprøvet på boardet. Stiplede forbindelser i systemdiagrammet viser integrationen af SNN og DFX.

## 1. Systemoverblik

```mermaid
flowchart TD
    subgraph pc["PC · evaluering"]
        data["Testbilleder og klasselabels"] --> sender["send_images.py"]
        sender -->|"JSONL med modtagne svar"| results["results.py"]
        results --> report["CSV og to confusion matrices"]
    end

    subgraph ps["PYNQ-Z2 · PS"]
        uart["ps_uart.c · modtagelse og svarrammer"]
        header["ps.h · fælles typer og API"]
        main["ps_main.c · hovedløkke og CNN-styring"]
        test["ps_uart_test.c · ekko og testsvar"]
        ddr["PS DDR · CNN-inputbuffer"]
        integration["SNN-forbehandling og DFX-styring · planlagt"]

        header -.- uart
        uart <-->|"Kørselsbuild"| main
        uart <-->|"Separat testbuild"| test
        main -->|"Konvertér og gem Q8.8"| ddr
        main -.->|"Udvid modelvalg og dispatch"| integration
    end

    subgraph pl["PYNQ-Z2 · PL"]
        cnn["CNN-IP · nuværende labdesign"]
        slot["DFX-slot · én aktiv CNN- eller SNN-IP"]
    end

    build["Træning, vægteksport og HLS/Vivado"]
    sender <-->|"UART · billeder og svar"| uart
    main <-->|"AXI-Lite · start, status og resultat"| cnn
    cnn -->|"AXI-master læser input"| ddr
    build -->|"CNN-bitstream og platform"| cnn
    build -.->|"Moduler og delvise bitstreams"| slot
    integration -.->|"Klargør input og vælg aktivt modul"| slot
```

- **Kørselsbuildet** består af `ps_main.c`, `ps_uart.c` og `ps.h`. PC’en sender de 4.800 rå gråtonepixels. PS’en opbevarer dem, mens CNN-grenen lægger et separat Q8.8-input i DDR, starter IP’en og henter resultatet.
- **Testbuildet** bruger `ps_uart_test.c` som indgangspunkt sammen med samme UART-modul og header. Det returnerer byte-ekko og markerede modelsvar. CNN-bitstream og formvægte hører til inferensforløbet.
- **Gruppens integration** tilføjer SNN’ens 384 features, dens accelerator og DFX-styringen ved modelvalget i `ps_main.c`. Den valgte SNN-inputbuffer og AXI-forbindelse afhænger af gruppens hardwaredesign. PC’en kan bruge den viste protokol i begge builds.

## 2. UART-rammer

```mermaid
flowchart TB
    subgraph request["PC → PS · Type 1 · 4.811 byte"]
        direction LR
        q1["A6 6A · 2 B"] --> q2["Header · 8 B"] --> q3["Modelmaske · 1 B"] --> q4["Rå pixels · 4.800 B"]
    end
    subgraph response["PS → PC · Type 2 · 37 byte pr. model"]
        direction LR
        r1["A5 · 1 B"] --> r2["Header · 8 B"] --> r3["Modelresultat · 27 B"] --> r4["5A · 1 B"]
    end
    request ~~~ response
```

Den fælles header er `body_length:u16 | version:u8 | type:u8 | image_id:u32`. Versionen er `1`. Længden tæller kun rammens body. Flerbytefelter er little-endian. Den opdaterede protokol bruger markører og længde til rammekontrol uden CRC.

| Type | Body i rækkefølge | Body | Hele rammen |
|---|---|---:|---:|
| **1 · Billede** | `model_mask:u8` → `pixels:u8[4800]` | 4.801 B | 4.811 B |
| **2 · Modelresultat** | `model_id:u8` → `status:u8` → `class_id:u8` → `infer_us:u32` → `prep_us:u32` → `scores:i32[4]` | 27 B | 37 B |
| **3 · Diagnose** | `status:u8` | 1 B | 11 B |
| **3 · Billedekko** | `status:u8=0` → `pixels:u8[4800]` | 4.801 B | 4.811 B |
| **4 · DFX-tid** | `model_id:u8` → `dfx_us:u32` | 5 B | 15 B |

På pc-siden vælger modelmasken `0` for testekko, `1` for CNN, `2` for SNN og `3` for begge. Modelsvar bruger model-ID `1` for CNN og `2` for SNN. Type 2-status `0` angiver et gyldigt resultat, `3` en utilgængelig model, `4` en inferensfejl og `128` et testsvar. Type 3 bruges blandt andet ved forkert længde (status `1`) eller ugyldig modelmaske (status `2`).

## 3. UART-protokol

```mermaid
sequenceDiagram
    participant PC as send_images.py
    participant U as ps_uart.c
    participant PS as Aktiv PS-app
    participant FPGA as FPGA-IP

    PC->>U: Type 1 · billed-ID, modelmaske og pixels
    Note over U: Kontrollér markør, version, type, længde og maske
    alt Ugyldig ramme med kendt billed-ID
        U-->>PC: Type 3 · diagnosestatus
    else Gyldigt billede
        U->>PS: Lever billed-ID, modelmaske og pixelbuffer
        alt Maske 0
            PS->>U: Ekko i testbuildet eller status 8 i kørselsbuildet
            U-->>PC: Type 3 · pixels eller diagnosestatus
        else Maske 1, 2 eller 3
            loop Ét svar pr. ønsket model
                alt Kørselsbuild
                    opt DFX integreret og skift udført
                        PS->>FPGA: Skift modul og kontrollér tilstand
                        PS->>U: Log skiftetid
                        U-->>PC: Type 4 · skiftetid før modelsvar
                    end
                    alt Model klar
                        PS->>FPGA: Klargør input og start IP
                        FPGA-->>PS: Resultat ved færdig inferens
                        Note over PS: Timeout giver fejlstatus
                    else Model utilgængelig
                        Note over PS: Sæt fejlstatus for modellen
                    end
                else Testbuild
                    Note over PS: Lav markerede testværdier
                end
                PS->>U: Send modelsvar
                U-->>PC: Type 2 · samme billed-ID og modellens ID
            end
            Note over PC: Validér og gem ét svar pr. forventet model-ID
        end
    end
```

PC’en holder én forespørgsel ad gangen åben ved **4 Mbaud, 8N1**. Med maske `3` hører to type 2-svar til samme billed-ID, og rækkefølgen kan skifte. PC’en tjekker startmarkør, header og slutmarkør, gemmer svarene i JSONL og går videre, når alle forventede model-ID’er er modtaget. Ved timeout rapporterer den de manglende svar.

Ved **UART-test** laver PC’en først to fejltests og kører derefter separat ekko og testmodelsvar for hvert billede. Ved **demo** bruger PC’en maske `3`, når begge modeller er integreret. Ved **benchmark** bruger den maske `1` og derefter `2` på samme testliste. Type 4 er beregnet til en særskilt måling pr. DFX-skift. De faktiske skift skal tilføjes af gruppen.
