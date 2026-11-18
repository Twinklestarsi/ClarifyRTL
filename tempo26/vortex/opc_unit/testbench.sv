`timescale 1ns / 1ps
`default_nettype none

module VX_opc_unit_tb;
    localparam integer DATAW = 32;
    localparam integer LANES = 4;
    localparam integer REGS = 32;
    localparam integer CONTEXTS = 4;
    localparam integer BANKS = 4;
    localparam integer VECTORW = LANES * DATAW;

    logic clk = 1'b0;
    logic reset;
    logic wb_valid;
    logic [1:0] wb_context;
    logic [4:0] wb_rd;
    logic [3:0] wb_tmask;
    logic [VECTORW-1:0] wb_data;
    logic issue_valid;
    wire issue_ready;
    logic [2:0] issue_used_rs;
    logic [4:0] issue_rs1;
    logic [4:0] issue_rs2;
    logic [4:0] issue_rs3;
    logic [1:0] issue_context;
    logic [3:0] issue_tmask;
    logic [7:0] issue_tag;
    logic [31:0] issue_pc;
    logic [7:0] issue_op;
    logic [4:0] issue_rd;
    logic issue_wb;
    logic issue_sop;
    logic issue_eop;
    wire operands_valid;
    logic operands_ready;
    wire [VECTORW-1:0] operand1;
    wire [VECTORW-1:0] operand2;
    wire [VECTORW-1:0] operand3;
    wire [1:0] operand_context;
    wire [3:0] operand_tmask;
    wire [7:0] operand_tag;
    wire [31:0] operand_pc;
    wire [7:0] operand_op;
    wire [4:0] operand_rd;
    wire operand_wb;
    wire operand_sop;
    wire operand_eop;
    wire [15:0] collision_count;

    logic [VECTORW-1:0] model_regs [0:CONTEXTS-1][0:REGS-1];
    integer errors;
    integer checks;
    integer c;
    integer r;
    integer lane;

    VX_opc_unit #(
        .DATAW(DATAW),
        .NUM_LANES(LANES),
        .NUM_REGS(REGS),
        .NUM_CONTEXTS(CONTEXTS),
        .NUM_BANKS(BANKS)
    ) dut (.*);

    always #5 clk = ~clk;

    task automatic fail(input string message);
        begin
            $display("ERROR %0t: %s", $time, message);
            errors = errors + 1;
        end
    endtask

    task automatic write_reg(
        input [1:0] ctx,
        input [4:0] reg_number,
        input [3:0] mask,
        input [VECTORW-1:0] data
    );
        integer index;
        begin
            @(negedge clk);
            wb_context = ctx;
            wb_rd = reg_number;
            wb_tmask = mask;
            wb_data = data;
            wb_valid = 1'b1;
            @(posedge clk);
            #1 wb_valid = 1'b0;
            if (reg_number != 0) begin
                for (index = 0; index < LANES; index = index + 1) begin
                    if (mask[index])
                        model_regs[ctx][reg_number][index*DATAW +: DATAW]
                            = data[index*DATAW +: DATAW];
                end
            end
        end
    endtask

    function automatic [VECTORW-1:0] expected_source;
        input used;
        input [1:0] ctx;
        input [4:0] reg_number;
        begin
            if (!used || reg_number == 0)
                expected_source = '0;
            else
                expected_source = model_regs[ctx][reg_number];
        end
    endfunction

    task automatic issue_and_check(
        input [2:0] used,
        input [4:0] rs1,
        input [4:0] rs2,
        input [4:0] rs3,
        input [1:0] ctx,
        input [7:0] tag,
        input integer expected_latency
    );
        logic [VECTORW-1:0] expected1;
        logic [VECTORW-1:0] expected2;
        logic [VECTORW-1:0] expected3;
        integer delay;
        begin
            expected1 = expected_source(used[0], ctx, rs1);
            expected2 = expected_source(used[1], ctx, rs2);
            expected3 = expected_source(used[2], ctx, rs3);
            @(negedge clk);
            issue_used_rs = used;
            issue_rs1 = rs1;
            issue_rs2 = rs2;
            issue_rs3 = rs3;
            issue_context = ctx;
            issue_tmask = tag[3:0];
            issue_tag = tag;
            issue_pc = {24'h123456, tag};
            issue_op = tag ^ 8'h5a;
            issue_rd = tag[4:0];
            issue_wb = tag[0];
            issue_sop = tag[1];
            issue_eop = tag[2];
            issue_valid = 1'b1;
            while (!issue_ready)
                @(negedge clk);
            @(posedge clk);
            #1 issue_valid = 1'b0;
            for (delay = 1; delay < expected_latency; delay = delay + 1) begin
                @(posedge clk);
                #1;
                if (operands_valid)
                    fail("operand packet appeared before specified bank latency");
            end
            @(posedge clk);
            #1;
            if (!operands_valid)
                fail("operand packet missing at specified bank latency");
            while (!operands_valid) begin
                @(posedge clk);
                #1;
            end
            if (operand1 !== expected1)
                fail("rs1 value mismatch");
            if (operand2 !== expected2)
                fail("rs2 value mismatch");
            if (operand3 !== expected3)
                fail("rs3 value mismatch");
            if (operand_context !== ctx || operand_tag !== tag)
                fail("context or tag mismatch");
            if (operand_tmask !== tag[3:0])
                fail("thread mask mismatch");
            if (operand_pc !== {24'h123456, tag})
                fail("program counter mismatch");
            if (operand_op !== (tag ^ 8'h5a) || operand_rd !== tag[4:0])
                fail("operation or destination mismatch");
            if (operand_wb !== tag[0] || operand_sop !== tag[1]
             || operand_eop !== tag[2])
                fail("control metadata mismatch");
            checks = checks + 1;
            @(posedge clk);
            #1;
        end
    endtask

    task automatic check_stall_stability;
        logic [VECTORW-1:0] held1;
        logic [VECTORW-1:0] held2;
        logic [7:0] held_tag;
        integer delay;
        begin
            operands_ready = 1'b0;
            @(negedge clk);
            issue_used_rs = 3'b011;
            issue_rs1 = 5'd2;
            issue_rs2 = 5'd3;
            issue_rs3 = 5'd0;
            issue_context = 2'd0;
            issue_tmask = 4'b1010;
            issue_tag = 8'he1;
            issue_pc = 32'h80000100;
            issue_op = 8'h77;
            issue_rd = 5'd17;
            issue_wb = 1'b1;
            issue_sop = 1'b1;
            issue_eop = 1'b0;
            issue_valid = 1'b1;
            while (!issue_ready)
                @(negedge clk);
            @(posedge clk);
            #1 issue_valid = 1'b0;
            @(posedge clk);
            #1;
            if (!operands_valid)
                fail("stalled packet did not arrive");
            held1 = operand1;
            held2 = operand2;
            held_tag = operand_tag;
            for (delay = 0; delay < 3; delay = delay + 1) begin
                @(posedge clk);
                #1;
                if (!operands_valid || operand1 !== held1
                 || operand2 !== held2 || operand_tag !== held_tag)
                    fail("FIFO output changed under backpressure");
            end
            operands_ready = 1'b1;
            @(posedge clk);
            #1;
            if (operands_valid)
                fail("stalled packet did not retire");
            checks = checks + 1;
        end
    endtask

    task automatic check_read_write_forwarding;
        logic [VECTORW-1:0] forwarded;
        begin
            forwarded = {32'h44440004, 32'h33330003,
                         32'h22220002, 32'h11110001};
            @(negedge clk);
            issue_used_rs = 3'b001;
            issue_rs1 = 5'd7;
            issue_rs2 = 5'd0;
            issue_rs3 = 5'd0;
            issue_context = 2'd2;
            issue_tmask = 4'b1111;
            issue_tag = 8'hc7;
            issue_pc = 32'h80000200;
            issue_op = 8'h39;
            issue_rd = 5'd20;
            issue_wb = 1'b1;
            issue_sop = 1'b0;
            issue_eop = 1'b1;
            issue_valid = 1'b1;
            while (!issue_ready)
                @(negedge clk);
            @(posedge clk);
            #1 issue_valid = 1'b0;

            @(negedge clk);
            wb_context = 2'd2;
            wb_rd = 5'd7;
            wb_tmask = 4'b1111;
            wb_data = forwarded;
            wb_valid = 1'b1;
            @(posedge clk);
            #1 wb_valid = 1'b0;
            model_regs[2][7] = forwarded;
            if (!operands_valid)
                fail("forwarded operand packet is late");
            if (operand1 !== forwarded)
                fail("same-cycle read/write forwarding mismatch");
            if (operand_tag !== 8'hc7)
                fail("forwarding packet metadata mismatch");
            @(posedge clk);
            #1;
            checks = checks + 1;
        end
    endtask

    initial begin
        errors = 0;
        checks = 0;
        for (c = 0; c < CONTEXTS; c = c + 1)
            for (r = 0; r < REGS; r = r + 1)
                model_regs[c][r] = '0;
        reset = 1'b1;
        wb_valid = 1'b0;
        wb_context = '0;
        wb_rd = '0;
        wb_tmask = '0;
        wb_data = '0;
        issue_valid = 1'b0;
        issue_used_rs = '0;
        issue_rs1 = '0;
        issue_rs2 = '0;
        issue_rs3 = '0;
        issue_context = '0;
        issue_tmask = '0;
        issue_tag = '0;
        issue_pc = '0;
        issue_op = '0;
        issue_rd = '0;
        issue_wb = 1'b0;
        issue_sop = 1'b0;
        issue_eop = 1'b0;
        operands_ready = 1'b1;
        repeat (3) @(posedge clk);
        @(negedge clk) reset = 1'b0;
        #1;
        checks = checks + 3;
        if (!issue_ready)
            fail("collector was not ready after reset");
        if (operands_valid)
            fail("operand FIFO valid was not cleared by reset");
        if (collision_count != 0)
            fail("collision counter did not reset");

        write_reg(2'd0, 5'd1, 4'b1111, {32'd14,32'd13,32'd12,32'd11});
        write_reg(2'd0, 5'd2, 4'b1111, {32'd24,32'd23,32'd22,32'd21});
        write_reg(2'd0, 5'd3, 4'b0101, {32'd34,32'd33,32'd32,32'd31});
        write_reg(2'd0, 5'd5, 4'b1111, {32'd54,32'd53,32'd52,32'd51});
        write_reg(2'd0, 5'd9, 4'b1111, {32'd94,32'd93,32'd92,32'd91});
        write_reg(2'd1, 5'd1, 4'b1111, {32'd104,32'd103,32'd102,32'd101});
        write_reg(2'd0, 5'd0, 4'b1111, {32'hdeadbeef,32'hdeadbeef,
                                                    32'hdeadbeef,32'hdeadbeef});

        issue_and_check(3'b111, 5'd1, 5'd2, 5'd3, 2'd0, 8'h11, 1);
        issue_and_check(3'b111, 5'd1, 5'd5, 5'd9, 2'd0, 8'h22, 3);
        issue_and_check(3'b101, 5'd0, 5'd7, 5'd1, 2'd0, 8'h33, 1);
        issue_and_check(3'b001, 5'd1, 5'd0, 5'd0, 2'd1, 8'h44, 1);

        write_reg(2'd0, 5'd3, 4'b1010, {32'd134,32'd133,32'd132,32'd131});
        issue_and_check(3'b001, 5'd3, 5'd0, 5'd0, 2'd0, 8'h55, 1);
        check_read_write_forwarding();

        if (collision_count < 2)
            fail("bank collision counter did not advance");
        check_stall_stability();

        if (errors != 0)
            $fatal(1, "VX_opc_unit failed with %0d errors", errors);
        $display("PASS VX_opc_unit: %0d checks, collisions=%0d",
                 checks, collision_count);
        $finish;
    end
endmodule

`default_nettype wire
