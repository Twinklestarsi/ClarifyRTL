`timescale 1ns / 1ps
`default_nettype none

// Standalone, banked Vortex-style local-memory subsystem.  The original
// interface arrays are flattened into packed SystemVerilog vectors, while the
// request crossbar, byte-write banks, and response crossbar remain explicit.
module VX_local_mem #(
    parameter integer DATAW      = 32,
    parameter integer SIZE_WORDS = 64,
    parameter integer NUM_REQS   = 4,
    parameter integer NUM_BANKS  = 4,
    parameter integer TAGW       = 8,
    parameter integer ADDRW      = $clog2(SIZE_WORDS),
    parameter integer BYTEENW    = DATAW / 8,
    parameter integer REQ_IDXW   = (NUM_REQS > 1) ? $clog2(NUM_REQS) : 1,
    parameter integer BANK_IDXW  = (NUM_BANKS > 1) ? $clog2(NUM_BANKS) : 1,
    parameter integer BANK_ADDRW = $clog2(SIZE_WORDS / NUM_BANKS),
    parameter integer PERF_W     = 32,
    parameter integer TIMING_BUG = 0
) (
    input  wire                              clk,
    input  wire                              reset,

    input  wire [NUM_REQS-1:0]               req_valid,
    output logic [NUM_REQS-1:0]               req_ready,
    input  wire [NUM_REQS-1:0]               req_write,
    input  wire [NUM_REQS-1:0][ADDRW-1:0]    req_addr,
    input  wire [NUM_REQS-1:0][BYTEENW-1:0]  req_byteen,
    input  wire [NUM_REQS-1:0][DATAW-1:0]    req_wdata,
    input  wire [NUM_REQS-1:0][TAGW-1:0]     req_tag,

    output logic [NUM_REQS-1:0]               rsp_valid,
    input  wire [NUM_REQS-1:0]               rsp_ready,
    output logic [NUM_REQS-1:0][DATAW-1:0]    rsp_rdata,
    output logic [NUM_REQS-1:0][TAGW-1:0]     rsp_tag,

    output logic [PERF_W-1:0]                 perf_reads,
    output logic [PERF_W-1:0]                 perf_writes,
    output logic [PERF_W-1:0]                 perf_bank_stalls,
    output logic [PERF_W-1:0]                 perf_rsp_stalls
);

    localparam integer WORDS_PER_BANK = SIZE_WORDS / NUM_BANKS;

    // Each bank is an independent byte-write synchronous memory.
    logic [DATAW-1:0] bank_mem [0:NUM_BANKS-1][0:WORDS_PER_BANK-1];

    // Request-side decode and round-robin arbitration state.
    logic [NUM_REQS-1:0][BANK_IDXW-1:0]  req_bank;
    logic [NUM_REQS-1:0][BANK_ADDRW-1:0] req_row;
    logic [NUM_BANKS-1:0][REQ_IDXW-1:0]  bank_grant_idx;
    logic [NUM_BANKS-1:0]                bank_grant_valid;
    logic [NUM_BANKS-1:0][REQ_IDXW-1:0]  bank_rr_ptr;
    logic [NUM_BANKS-1:0][REQ_IDXW-1:0]  bank_rr_next;

    // A read owns its requester slot until its response is consumed.  Writes
    // have no response and therefore do not allocate an outstanding slot.
    logic [NUM_REQS-1:0] requester_busy;

    // One elastic response entry per bank decouples RAM access from the
    // requester.  This is the same role as the BRAM pipe buffers in Vortex.
    logic [NUM_BANKS-1:0]                 bank_rsp_valid;
    logic [NUM_BANKS-1:0]                 bank_rsp_reveal;
    logic [NUM_BANKS-1:0][DATAW-1:0]      bank_rsp_data;
    logic [NUM_BANKS-1:0][TAGW-1:0]       bank_rsp_tag;
    logic [NUM_BANKS-1:0][REQ_IDXW-1:0]   bank_rsp_dst;
    logic [NUM_BANKS-1:0]                 bank_rsp_ready;
    logic [NUM_BANKS-1:0]                 bank_rsp_visible;

    // Response-side arbitration is needed when several banks complete reads
    // for the same requester in the same cycle.
    logic [NUM_REQS-1:0][BANK_IDXW-1:0] rsp_grant_bank;
    logic [NUM_REQS-1:0]                rsp_grant_valid;
    logic [NUM_REQS-1:0][BANK_IDXW-1:0] rsp_rr_ptr;
    logic [NUM_REQS-1:0][BANK_IDXW-1:0] rsp_rr_next;

    logic [NUM_BANKS-1:0] bank_can_accept;
    logic [NUM_BANKS-1:0] bank_fire;
    logic [NUM_REQS-1:0]  req_fire;
    logic [NUM_REQS-1:0]  rsp_fire;
    logic [NUM_REQS-1:0]  request_eligible;

    integer b;
    integer r;
    integer k;
    integer candidate;
    integer byte_index;
    integer reads_this_cycle;
    integer writes_this_cycle;
    integer conflicts_this_cycle;
    integer stalls_this_cycle;

    initial begin
        if (DATAW < 8 || (DATAW % 8) != 0)
            $fatal(1, "DATAW must contain whole bytes");
        if (NUM_REQS < 1 || NUM_BANKS < 1)
            $fatal(1, "NUM_REQS and NUM_BANKS must be positive");
        if ((SIZE_WORDS % NUM_BANKS) != 0)
            $fatal(1, "SIZE_WORDS must divide evenly across banks");
        if ((1 << BANK_IDXW) < NUM_BANKS)
            $fatal(1, "BANK_IDXW is too small");
    end

    // Low word-address bits select the bank; upper bits select its row.  This
    // interleaving allows adjacent lanes to access different banks in parallel.
    always_comb begin
        for (r = 0; r < NUM_REQS; r = r + 1) begin
            if (NUM_BANKS == 1)
                req_bank[r] = '0;
            else
                req_bank[r] = req_addr[r][BANK_IDXW-1:0];
            req_row[r] = req_addr[r][ADDRW-1:BANK_IDXW];
            request_eligible[r] = req_valid[r]
                               && (req_write[r] || !requester_busy[r]);
        end
    end

    // A response entry may be replaced in the cycle in which the old entry is
    // accepted.  Writes never consume response capacity.
    always_comb begin
        for (b = 0; b < NUM_BANKS; b = b + 1) begin
            // MUTATION POINT: TIMING_BUG delays read visibility by one clock.
            bank_rsp_visible[b] = bank_rsp_valid[b]
                                && (!TIMING_BUG || bank_rsp_reveal[b]);
            bank_can_accept[b] = !bank_rsp_valid[b] || bank_rsp_ready[b];
        end
    end

    // Request crossbar: independent rotating-priority arbiter for every bank.
    // A stalled read bank is still permitted to accept writes, because writes
    // generate no response and the memory itself remains single-cycle capable.
    always_comb begin
        bank_grant_valid = '0;
        bank_grant_idx   = '0;
        bank_rr_next     = bank_rr_ptr;

        for (b = 0; b < NUM_BANKS; b = b + 1) begin
            for (k = 0; k < NUM_REQS; k = k + 1) begin
                candidate = bank_rr_ptr[b] + k;
                if (candidate >= NUM_REQS)
                    candidate = candidate - NUM_REQS;
                if (!bank_grant_valid[b]
                 && request_eligible[candidate]
                 && (req_bank[candidate] == BANK_IDXW'(b))
                 && (req_write[candidate] || bank_can_accept[b])) begin
                    bank_grant_valid[b] = 1'b1;
                    bank_grant_idx[b]   = REQ_IDXW'(candidate);
                    if (candidate == (NUM_REQS - 1))
                        bank_rr_next[b] = '0;
                    else
                        bank_rr_next[b] = REQ_IDXW'(candidate + 1);
                end
            end
        end
    end

    // Ready is asserted only for the request actually selected by its bank.
    // This naturally exposes same-bank collisions to the losing requesters.
    always_comb begin
        req_ready = '0;
        bank_fire = '0;
        for (b = 0; b < NUM_BANKS; b = b + 1) begin
            if (bank_grant_valid[b]) begin
                req_ready[bank_grant_idx[b]] = 1'b1;
                bank_fire[b] = req_valid[bank_grant_idx[b]];
            end
        end
        req_fire = req_valid & req_ready;
    end

    // Response crossbar: independently arbitrate responses destined for each
    // request port.  Data and tag are held stable whenever rsp_ready is low.
    always_comb begin
        rsp_valid       = '0;
        rsp_rdata       = '0;
        rsp_tag         = '0;
        rsp_grant_valid = '0;
        rsp_grant_bank  = '0;
        rsp_rr_next     = rsp_rr_ptr;
        bank_rsp_ready  = '0;

        for (r = 0; r < NUM_REQS; r = r + 1) begin
            for (k = 0; k < NUM_BANKS; k = k + 1) begin
                candidate = rsp_rr_ptr[r] + k;
                if (candidate >= NUM_BANKS)
                    candidate = candidate - NUM_BANKS;
                if (!rsp_grant_valid[r]
                 && bank_rsp_visible[candidate]
                 && (bank_rsp_dst[candidate] == REQ_IDXW'(r))) begin
                    rsp_grant_valid[r] = 1'b1;
                    rsp_grant_bank[r]  = BANK_IDXW'(candidate);
                    if (candidate == (NUM_BANKS - 1))
                        rsp_rr_next[r] = '0;
                    else
                        rsp_rr_next[r] = BANK_IDXW'(candidate + 1);
                end
            end

            if (rsp_grant_valid[r]) begin
                rsp_valid[r] = 1'b1;
                rsp_rdata[r] = bank_rsp_data[rsp_grant_bank[r]];
                rsp_tag[r]   = bank_rsp_tag[rsp_grant_bank[r]];
                bank_rsp_ready[rsp_grant_bank[r]] = rsp_ready[r];
            end
        end
        rsp_fire = rsp_valid & rsp_ready;
    end

    // Sequential bank storage and elastic response state.
    always_ff @(posedge clk) begin
        if (reset) begin
            bank_rsp_valid  <= '0;
            bank_rsp_reveal <= '0;
            bank_rsp_data   <= '0;
            bank_rsp_tag    <= '0;
            bank_rsp_dst    <= '0;
            bank_rr_ptr     <= '0;
            rsp_rr_ptr      <= '0;
            requester_busy  <= '0;
        end else begin
            for (r = 0; r < NUM_REQS; r = r + 1) begin
                if (rsp_fire[r]) begin
                    requester_busy[r] <= 1'b0;
                    rsp_rr_ptr[r] <= rsp_rr_next[r];
                end
                if (req_fire[r] && !req_write[r])
                    requester_busy[r] <= 1'b1;
            end

            for (b = 0; b < NUM_BANKS; b = b + 1) begin
                if (bank_rsp_valid[b])
                    bank_rsp_reveal[b] <= 1'b1;

                if (bank_rsp_ready[b] && bank_rsp_visible[b]) begin
                    bank_rsp_valid[b]  <= 1'b0;
                    bank_rsp_reveal[b] <= 1'b0;
                end

                if (bank_fire[b]) begin
                    bank_rr_ptr[b] <= bank_rr_next[b];
                    if (req_write[bank_grant_idx[b]]) begin
                        for (byte_index = 0;
                             byte_index < BYTEENW;
                             byte_index = byte_index + 1) begin
                            if (req_byteen[bank_grant_idx[b]][byte_index])
                                bank_mem[b][req_row[bank_grant_idx[b]]]
                                    [byte_index*8 +: 8]
                                  <= req_wdata[bank_grant_idx[b]]
                                    [byte_index*8 +: 8];
                        end
                    end else begin
                        bank_rsp_valid[b]  <= 1'b1;
                        bank_rsp_reveal[b] <= 1'b0;
                        bank_rsp_data[b]
                          <= bank_mem[b][req_row[bank_grant_idx[b]]];
                        bank_rsp_tag[b]
                          <= req_tag[bank_grant_idx[b]];
                        bank_rsp_dst[b]
                          <= bank_grant_idx[b];
                    end
                end
            end
        end
    end

    // Performance counters retain the useful observability of VX_local_mem:
    // accepted accesses, arbitration collisions, and response backpressure.
    always_comb begin
        reads_this_cycle     = 0;
        writes_this_cycle    = 0;
        conflicts_this_cycle = 0;
        stalls_this_cycle    = 0;

        for (r = 0; r < NUM_REQS; r = r + 1) begin
            if (req_fire[r] && req_write[r])
                writes_this_cycle = writes_this_cycle + 1;
            if (req_fire[r] && !req_write[r])
                reads_this_cycle = reads_this_cycle + 1;
            if (req_valid[r] && !req_ready[r])
                conflicts_this_cycle = conflicts_this_cycle + 1;
            if (rsp_valid[r] && !rsp_ready[r])
                stalls_this_cycle = stalls_this_cycle + 1;
        end
    end

    always_ff @(posedge clk) begin
        if (reset) begin
            perf_reads       <= '0;
            perf_writes      <= '0;
            perf_bank_stalls <= '0;
            perf_rsp_stalls  <= '0;
        end else begin
            perf_reads       <= perf_reads + PERF_W'(reads_this_cycle);
            perf_writes      <= perf_writes + PERF_W'(writes_this_cycle);
            perf_bank_stalls <= perf_bank_stalls
                              + PERF_W'(conflicts_this_cycle);
            perf_rsp_stalls  <= perf_rsp_stalls
                              + PERF_W'(stalls_this_cycle);
        end
    end

    // Protocol safety properties are simulation-visible and synthesizable
    // tools simply ignore the diagnostic action.
    always_ff @(posedge clk) begin
        if (!reset) begin
            for (r = 0; r < NUM_REQS; r = r + 1) begin
                if (req_valid[r] && req_ready[r] && !req_write[r]
                 && requester_busy[r] && !rsp_fire[r])
                    $error("requester accepted a second outstanding read");
            end
        end
    end

endmodule

`default_nettype wire
